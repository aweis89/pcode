"""Mutation evidence adapted from Harness 0.31.0's filesystem operations.

Only the two mutation bodies are adapted: keep descriptor validation, conflict
checks, newline semantics and result strings aligned with that installed release.
Snapshots are taken inside the operation, not from a later workspace reread.
"""

import errno
import os
import stat
from dataclasses import dataclass, fields
from pathlib import Path

from pydantic_ai import CapabilityEvent
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.tools import AgentDepsT, RunContext
from pydantic_ai_harness.filesystem import FileSystem
from pydantic_ai_harness.filesystem._events import FileWrittenEvent
from pydantic_ai_harness.filesystem._toolset import (
    FileSystemToolset,
    _content_hash,
    _read_canonical_text,
    _recoverable,
)

from pcode.edits import MAX_SOURCE, completed_change, sensitive_path
from pcode.runtime import EditCompleted


@dataclass(kw_only=True)
class FileChangeEvent(CapabilityEvent, namespace="pcode_files", name="change"):
    change: EditCompleted


class DisplayFileSystem(FileSystem):
    @classmethod
    def from_filesystem(cls, filesystem):
        return cls(**{f.name: getattr(filesystem, f.name) for f in fields(FileSystem) if f.init})

    def get_toolset(self):
        if self.read_only:
            return super().get_toolset()
        return DisplayFileSystemToolset(
            root_dir=Path(self.root_dir),
            allowed_patterns=self.allowed_patterns,
            denied_patterns=self.denied_patterns,
            protected_patterns=self.protected_patterns,
            max_read_lines=self.max_read_lines,
            max_list_results=self.max_list_results,
            max_search_results=self.max_search_results,
            max_find_results=self.max_find_results,
            id=self.id or "file_system",
        )


class DisplayFileSystemToolset(FileSystemToolset):
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
        display_path = path if sensitive_path(path) else self._event_location(resolved)["path"]

        if resolved.exists() and not resolved.is_file():
            raise ModelRetry(f"Path {path!r} exists and is not a regular file.")

        if not resolved.parent.exists():
            parent_rel = str(resolved.parent.relative_to(self._root))
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
        return f"Wrote {len(content)} chars ({lines} lines) to {path}. [hash:{new_hash}]"

    @_recoverable
    async def _edit_file(
        self,
        ctx: RunContext[AgentDepsT] | None,
        path: str,
        old_text: str,
        new_text: str,
        *,
        expected_hash: str | None = None,
    ) -> str:
        resolved = self._safe_resolve(path, write=True)
        display_path = path if sensitive_path(path) else self._event_location(resolved)["path"]
        if not resolved.is_file():
            raise FileNotFoundError(f"File not found: {path}")

        # Reading and writing with `newline=''` disables universal-newline
        # translation, so the text is the canonical bytes-on-disk view that
        # `read_file` hashes, and the replacement preserves `\r\n` exactly
        # instead of writing `\r\r\n` through a translating writer on Windows.
        text = _read_canonical_text(resolved)
        current_hash = _content_hash(text)

        # Optimistic concurrency check
        if expected_hash is not None and current_hash != expected_hash:
            raise ValueError(
                f"Conflict: file {path!r} has changed (expected hash:{expected_hash}, "
                f"got hash:{current_hash}). Re-read the file and retry."
            )

        count = text.count(old_text)
        if count == 0:
            raise ValueError(f"old_text not found in {path}.")
        if count > 1:
            raise ValueError(
                f"old_text found {count} times in {path}. "
                "Include more surrounding context to make the match unique."
            )

        new_content = text.replace(old_text, new_text, 1)
        resolved.write_text(new_content, encoding="utf-8", newline="")
        if ctx is not None:
            await self._emit_change(ctx, display_path, text, new_content)
        new_hash = _content_hash(new_content)
        if ctx is not None:
            await ctx.emit(
                FileWrittenEvent(**self._event_location(resolved), content_hash=new_hash)
            )
        return f"Edited {path}. [hash:{new_hash}]"
