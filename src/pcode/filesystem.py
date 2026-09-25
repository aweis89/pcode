"""Mutation evidence layered on Harness's filesystem operations.

The write capture retains its 0.31.0 descriptor-based snapshot. Edits follow the
pinned upstream replacement batches, announcement, and guarded write. Snapshots
are taken inside the operation, not from a later workspace reread.
"""

import errno
import os
import stat
from dataclasses import dataclass

from pydantic_ai import CapabilityEvent
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.tools import AgentDepsT, RunContext
from pydantic_ai_harness.filesystem._changes import Change
from pydantic_ai_harness.filesystem._events import FileWrittenEvent
from pydantic_ai_harness.filesystem._toolset import (
    _apply_replacements,
    _content_hash,
    _is_binary,
    _recoverable,
    _write_content,
)

from pcode.edits import MAX_SOURCE, completed_change, sensitive_path
from pcode.runtime import EditCompleted
from pcode.workspace_filesystem import WorkspaceFileSystem, WorkspaceFileSystemToolset


@dataclass(kw_only=True)
class FileChangeEvent(CapabilityEvent, namespace="pcode_files", name="change"):
    change: EditCompleted


class DisplayFileSystem(WorkspaceFileSystem):
    def _toolset_type(self):
        return DisplayFileSystemToolset


class DisplayFileSystemToolset(WorkspaceFileSystemToolset):
    async def _emit_change(self, ctx, path, before, after, **kwargs):
        change = completed_change(path, before, after, call_id=ctx.tool_call_id or "", **kwargs)
        await ctx.emit(FileChangeEvent(change=change))

    @_recoverable
    async def _write_file(
        self,
        ctx: RunContext[AgentDepsT] | None,
        path: str,
        content: str,
        *,
        expected_hash: str | None = None,
    ) -> str:
        resolved = self._safe_resolve(path, write=True)
        display_path = path if sensitive_path(path) else self._relative_to_root(resolved)

        if resolved.exists() and not resolved.is_file():
            raise ModelRetry(f"Path {path!r} exists and is not a regular file.")

        if not resolved.parent.exists():
            parent_rel = self._relative_to_root(resolved.parent)
            raise FileNotFoundError(
                f"Parent directory '{parent_rel}' does not exist. Use create_directory first."
            )

        # Opening without O_TRUNC lets us classify the descriptor and check the
        # expected hash before changing the file. POSIX non-blocking mode keeps
        # a FIFO swapped into place from waiting for a reader; O_NOFOLLOW keeps
        # a final-component symlink swap from redirecting the descriptor. Windows
        # has no filesystem FIFO equivalent, and O_BINARY plus `newline=''` on
        # the text wrapper means the written bytes reproduce the content
        # argument exactly: no newline translation, so the reported hash always
        # matches the bytes a later `read_file` hashes.
        platform_flags = os.O_BINARY if os.name == "nt" else os.O_NONBLOCK | os.O_NOFOLLOW
        capture = ctx is not None and not sensitive_path(display_path)
        readable = capture or expected_hash is not None
        access_flags = os.O_RDWR if readable else os.O_WRONLY
        before = None
        omitted = ""

        def open_destination(flags):
            nonlocal readable
            try:
                return os.open(resolved, flags, 0o666)
            except PermissionError:
                if expected_hash is not None or not capture:
                    raise
                # Display evidence must not require permissions the write itself
                # doesn't need. Preserve write-only access and omit its diff.
                readable = False
                return os.open(resolved, (flags & ~os.O_RDWR) | os.O_WRONLY, 0o666)

        created = False
        descriptor = -1
        try:
            # The target can disappear after O_EXCL reports that it exists. Retry
            # the complete atomic classification so an ordinary write still
            # recreates it, while bounding churn from a concurrently replaced path.
            for _ in range(3):
                try:
                    descriptor = open_destination(
                        access_flags | platform_flags | os.O_CREAT | os.O_EXCL
                    )
                except FileExistsError:
                    try:
                        descriptor = open_destination(access_flags | platform_flags)
                    except FileNotFoundError:
                        continue
                else:
                    created = True
                break
            else:
                raise ModelRetry(
                    f"Path {path!r} changed repeatedly while opening. Retry the write."
                )
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise ModelRetry(
                    f"Path {path!r} encountered a symlink loop "
                    "or changed to a symlink before opening."
                ) from e
            if e.errno in (errno.EISDIR, errno.ENODEV, errno.ENXIO):
                raise ModelRetry(f"Path {path!r} exists and is not a regular file.") from e
            raise

        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ModelRetry(f"Path {path!r} exists and is not a regular file.")

            mode = "r+" if readable else "w"
            text_file = os.fdopen(descriptor, mode, encoding="utf-8", newline="")
            descriptor = -1
            with text_file:
                if expected_hash is not None and not created:
                    checked_text = text_file.read()
                    current_hash = _content_hash(checked_text)
                    if current_hash != expected_hash:
                        raise ValueError(
                            f"Conflict: file {path!r} has changed (expected hash:{expected_hash}, "
                            f"got hash:{current_hash}). Re-read the file and retry."
                        )

                if created:
                    before = ""
                elif capture and expected_hash is not None:
                    before = checked_text
                elif capture and readable:
                    text_file.seek(0)
                    try:
                        before = text_file.read(MAX_SOURCE + 1)
                    except UnicodeDecodeError:
                        omitted = "Binary or non-UTF-8 content"
                text_file.seek(0)
                text_file.truncate(0)
                text_file.write(content)
        finally:
            if descriptor >= 0:
                os.close(descriptor)

        if ctx is not None:
            await self._emit_change(
                ctx, display_path, before, content, existed=not created, omitted=omitted
            )
        new_hash = _content_hash(content)
        lines = len(content.splitlines())
        if ctx is not None:
            await ctx.emit(
                FileWrittenEvent(**self._event_location(resolved), content_hash=new_hash)
            )
        return f"Wrote {len(content)} chars ({lines} lines) to {path}.{self._hash_suffix(new_hash)}"

    @_recoverable
    async def _edit_file(
        self,
        ctx: RunContext[AgentDepsT] | None,
        path: str,
        replacements,
        *,
        expected_hash: str | None = None,
    ) -> str:
        resolved = self._safe_resolve(path, write=True)
        display_path = path if sensitive_path(path) else self._relative_to_root(resolved)
        if not resolved.is_file():
            raise FileNotFoundError(f"File not found: {path}")

        with self.open_read(resolved) as source:
            head = source.read(8192)
            if _is_binary(head):
                raise ValueError(f"{path} is a binary file; edit_file only edits text files.")
            # Decoded from bytes, with no universal-newline translation, so the
            # text is the view `read_file` hashes and `\r\n` survives the edit.
            text = (head + source.read()).decode("utf-8")
        current_hash = _content_hash(text)

        # Optimistic concurrency check
        if expected_hash is not None and current_hash != expected_hash:
            raise ValueError(
                f"Conflict: file {path!r} has changed (expected hash:{expected_hash}, "
                f"got hash:{current_hash}). Re-read the file and retry."
            )

        new_content = _apply_replacements(text, replacements, path)
        change = Change.propose(
            **self._event_location(resolved), operation="edit", old=text, new=new_content
        )
        if (refusal := await self._request(ctx, change, path=path, resolved=resolved)) is not None:
            return refusal
        # The write re-checks the hash: a listener (an approval prompt) may have
        # held the edit long enough for the file to change underneath it.
        source, created = self.open_write(resolved, read_back=True, create=False)
        _write_content(source, path, new_content, expected_hash=current_hash, created=created)
        if ctx is not None:
            await self._emit_change(ctx, display_path, text, new_content)
        new_hash = _content_hash(new_content)
        if ctx is not None:
            await ctx.emit(change.edited(content_hash=new_hash))
        return f"Edited {path}.{self._hash_suffix(new_hash)}"
