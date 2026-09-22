"""Unconfined file paths with a stable workspace-relative base.

Harness 0.31's walkers inline root-relative conversions, so these three small
walkers track upstream while the read/write/edit implementations stay inherited.
See docs/dependencies.md before upgrading the filesystem integration.
"""

# Portions adapted from pydantic-ai-harness 0.31.0 filesystem/_toolset.py.
# The MIT License (MIT)
#
# Copyright (c) 2026 Pydantic Services Inc.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import errno
import fnmatch
import os
import re
from dataclasses import fields
from pathlib import Path

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.tools import AgentDepsT
from pydantic_ai.toolsets import FilteredToolset
from pydantic_ai_harness.filesystem import (
    READ_ONLY_TOOL_NAMES,
    DirectoryListedEvent,
    FileSystem,
    FileSystemToolset,
)
from pydantic_ai_harness.filesystem._toolset import (
    _EventLocation,
    _is_binary,
    _matching_lines,
    _recoverable,
)


class WorkspaceFileSystem(FileSystem):
    """Keep workspace-relative paths without using the workspace as a boundary."""

    @classmethod
    def from_filesystem(cls, filesystem: FileSystem) -> "WorkspaceFileSystem":
        return cls(**{f.name: getattr(filesystem, f.name) for f in fields(FileSystem) if f.init})

    def get_instructions(self) -> str:
        return (
            f"File tools accept absolute paths anywhere on the host and relative paths "
            f"based on the workspace {str(Path(self.root_dir).resolve())!r}, including '..'. "
            "Shell cd does not change that file-tool base. File tools are not a sandbox. "
            "Search and listing tools default to the workspace. "
            "Returned relative paths use that same workspace base. "
            "Existing protected-file write rules still apply to file tools, not shell commands. "
            # Measured over ~2k recorded edit_file calls: the flat form never failed
            # argument validation, while ~10% of `replacements` calls arrived as a
            # mangled JSON string ("Input should be a valid array") and cost a retry.
            # Three quarters of those carried a single edit that needed no array.
            "For edit_file, pass a single edit as top-level old_text/new_text; "
            "use the replacements array only for two or more edits to one file."
        )

    def _toolset_type(self):
        return WorkspaceFileSystemToolset

    def get_toolset(self):
        toolset = self._toolset_type()(
            root_dir=Path(self.root_dir),
            allowed_patterns=self.allowed_patterns,
            denied_patterns=self.denied_patterns,
            # Apply protected-file rules at any depth, including external paths.
            protected_patterns=[
                pattern if pattern.startswith("**/") else f"**/{pattern}"
                for pattern in self.protected_patterns
            ],
            max_read_lines=self.max_read_lines,
            max_read_chars=self.max_read_chars,
            cwd=None if self.cwd is None else Path(self.cwd),
            content_hashes=self.content_hashes,
            tools=self.tools,
            max_list_results=self.max_list_results,
            max_search_results=self.max_search_results,
            max_find_results=self.max_find_results,
            id=self.id or "file_system",
        )
        if self.read_only:
            return FilteredToolset(toolset, lambda ctx, tool: tool.name in READ_ONLY_TOOL_NAMES)
        return toolset


class WorkspaceFileSystemToolset(FileSystemToolset):
    def _resolve_path(self, path: str) -> Path:
        try:
            candidate = (self._cwd / path).resolve()
        except RuntimeError as exc:
            raise ModelRetry(f"Path {path!r} resolves through a symlink loop.") from exc
        if not candidate.exists():
            try:
                candidate.stat()
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise ModelRetry(f"Path {path!r} resolves through a symlink loop.") from exc
        return candidate

    def _relative_to_root(self, resolved: Path) -> str:
        if resolved.is_relative_to(self._real_root):
            return str(resolved.relative_to(self._real_root))
        return str(resolved)

    def _event_location(self, resolved: Path) -> _EventLocation:
        root = (
            self._real_root if resolved.is_relative_to(self._real_root) else Path(resolved.anchor)
        )
        return _EventLocation(path=str(resolved.relative_to(root)), root_dir=str(root))

    def _resolve_walk_entry(self, entry: Path) -> Path | None:
        try:
            target = self._resolve_path(str(entry))
        except (OSError, ModelRetry):
            return None
        return target if self._is_accessible(self._relative_to_root(target)) else None

    @_recoverable
    async def file_info(self, path: str) -> str:
        """Get metadata about a file or directory.

        Args:
            path: File or directory path, absolute or relative to the workspace.

        Returns:
            Formatted metadata including size, type, and permissions.
        """
        result = await super().file_info(path)
        if (self._root / path).is_symlink():
            target = self._relative_to_root(self._resolve_path(path))
            return "\n".join(
                f"symlink_target: {target}" if line.startswith("symlink_target:") else line
                for line in result.splitlines()
            )
        return result

    def _walk_relative(self, entry: Path, selected: Path) -> Path:
        # Preserve workspace matching; external walks ignore ancestors of the
        # explicitly selected tree (including hidden ancestors and '..').
        root = self._real_root
        if not selected.is_relative_to(root):
            root = selected if selected.is_dir() else selected.parent
        return entry.relative_to(root)

    @_recoverable
    async def _write_file(self, ctx, path, content, *, expected_hash=None):
        resolved = self._safe_resolve(path, write=True)
        if not resolved.parent.exists():
            raise ModelRetry(
                f"Parent directory {self._relative_to_root(resolved.parent)!r} "
                "does not exist. Use create_directory first."
            )
        return await super()._write_file(ctx, path, content, expected_hash=expected_hash)

    @_recoverable
    async def _list_directory(self, ctx: RunContext[AgentDepsT] | None, path: str = ".") -> str:
        # The listing root is gated by denied patterns but not by
        # allowed_patterns: a directory like '.' never matches a file pattern.
        # Entries are filtered per-entry against allowed_patterns below.
        resolved = self._safe_resolve(path, check_allowed=False)
        if not resolved.is_dir():
            raise NotADirectoryError(f"Not a directory: {path}")

        entries: list[str] = []
        entry_count = 0
        for entry in sorted(resolved.iterdir()):
            rel_path = self._walk_relative(entry, resolved)
            # Skip dotfiles and dot-directories, matching search_files and
            # find_files so the three walkers agree on what exists.
            if any(part.startswith(".") for part in rel_path.parts):
                continue
            target = self._resolve_walk_entry(entry)
            if target is None:
                continue
            rel = self._relative_to_root(entry)
            if target.is_dir():
                line = f"{rel}/"
            else:
                try:
                    size = target.stat().st_size
                except OSError:
                    # A dangling symlink, or an entry deleted mid-walk: it has
                    # no size to report, so leave it out of the listing.
                    continue
                line = f"{rel}  ({size} bytes)"
            # Only a listing that actually dropped an entry is marked truncated,
            # so one that merely fills the cap reads as complete.
            if len(entries) >= self._max_list_results:
                entries.append(f"[... truncated at {self._max_list_results} entries]")
                break
            entries.append(line)
            entry_count += 1
        if ctx is not None:
            await ctx.emit(
                DirectoryListedEvent(**self._event_location(resolved), entry_count=entry_count)
            )
        return "\n".join(entries) if entries else "(empty directory)"

    @_recoverable
    async def search_files(
        self, pattern: str, *, path: str = ".", include_glob: str | None = None
    ) -> str:
        """Search file contents using a regular expression.

        Args:
            pattern: Regex pattern to search for.
            path: Directory to search in, absolute or relative to the workspace.
            include_glob: If provided, only search files matching this glob (e.g. '*.py').

        Returns:
            str: Matching lines formatted as file:line_number:text.
        """
        # See list_directory: the search root isn't gated by allowed_patterns;
        # matched files are filtered per-entry below.
        resolved = self._safe_resolve(path, check_allowed=False)
        try:
            compiled = re.compile(pattern)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern: {e}") from e

        results: list[str] = []

        if resolved.is_file():
            files = [resolved]
        else:
            files = sorted(resolved.rglob("*"))

        for file_path in files:
            rel_path = self._walk_relative(file_path, resolved)
            if any(part.startswith(".") for part in rel_path.parts):
                continue
            rel_str = self._relative_to_root(file_path)
            if include_glob and not fnmatch.fnmatch(str(rel_path), include_glob):
                continue
            target = self._resolve_walk_entry(file_path)
            if target is None:
                continue
            if not target.is_file():
                continue
            try:
                raw = target.read_bytes()
            except OSError:  # pragma: no cover
                continue
            if _is_binary(raw):
                continue
            text = raw.decode("utf-8", errors="replace")
            matches, truncated = _matching_lines(
                text, compiled, rel_str, self._max_search_results - len(results)
            )
            results.extend(matches)
            if truncated:
                results.append(f"[... truncated at {self._max_search_results} matches]")
                break

        return "\n".join(results) if results else "No matches found."

    @_recoverable
    async def find_files(self, pattern: str, *, path: str = ".") -> str:
        """Find files by glob pattern (name matching, not content search).

        Args:
            pattern: Glob pattern to match, relative to `path` (e.g. '*.py',
                '**/*.json'). Absolute patterns are rejected.
            path: Directory to search in, absolute or relative to the workspace.

        Returns:
            Matching paths, workspace-relative inside it and absolute outside it.
        """
        if os.path.isabs(pattern):
            raise ValueError(
                f"Pattern {pattern!r} must be relative to the search path, not absolute."
            )

        # See list_directory: the find root isn't gated by allowed_patterns;
        # matched entries are filtered per-entry below.
        resolved = self._safe_resolve(path, check_allowed=False)
        if not resolved.is_dir():
            raise NotADirectoryError(f"Not a directory: {path}")

        try:
            found = sorted(resolved.glob(pattern))
        except NotImplementedError as e:
            # The `isabs` guard above takes a rooted pattern first on POSIX. On
            # Windows it does not: since 3.13 `os.path.isabs` reports a single
            # leading slash as relative, so `/etc/*.conf` reaches `glob`, which
            # rejects any rooted pattern. `NotImplementedError` is not an
            # `OSError`, so neither the recoverable tuple nor the errno table
            # can reach it.
            raise ModelRetry(
                f"Pattern {pattern!r} must be relative to {path!r}, not an absolute path."
            ) from e
        except IndexError as e:
            # Python 3.10 through 3.12 raise this for a pattern whose last
            # component is a bare `.`. On 3.13+ the same pattern raises
            # `ValueError`, which the recoverable tuple already covers.
            raise ModelRetry(f"Pattern {pattern!r} is not a valid glob pattern.") from e

        matches: list[str] = []
        for match in found:
            rel_path = self._walk_relative(match, resolved)
            if any(part.startswith(".") for part in rel_path.parts):
                continue
            target = self._resolve_walk_entry(match)
            if target is None:
                continue
            if not target.exists():
                # A dangling symlink resolves inside the root but names nothing.
                continue
            if len(matches) >= self._max_find_results:
                matches.append(f"[... truncated at {self._max_find_results} matches]")
                break
            rel = self._relative_to_root(match)
            suffix = "/" if target.is_dir() else ""
            matches.append(f"{rel}{suffix}")

        return "\n".join(matches) if matches else "No matches found."
