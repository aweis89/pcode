"""Workspace file references: type `@` in the prompt to insert a relative path.

Completing `@ui.py` inserts `./src/pcode/ui.py`, the same workspace-relative
form the file tools accept, so the model can read or search it without guessing
where the file lives. Short referenced files ride along with the request that
names them, sparing the model a read call; larger ones are named with their size
so it can decide whether reading them is worth it.
"""

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.lexers import Lexer

# Directories that never hold source worth referencing, used only when the
# workspace is not a Git checkout (Git supplies its own ignore rules).
IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "venv",
    }
)
MAX_FILES = 20000
MAX_COMPLETIONS = 50
CACHE_SECONDS = 10.0
# Inlining trades tokens for a round trip. These bounds keep a reference from
# quietly costing more than the read call it replaces.
INLINE_FILE_LIMIT = 16000
INLINE_TOTAL_LIMIT = 48000
INLINE_HEADER = "Referenced files, inlined by pcode when this message was sent:"

# A reference is a whitespace-delimited token: an unfinished `@fragment`, a
# `./` or `../` path, or a quoted path whose name contains spaces.
REFERENCE_PATTERN = re.compile(r'(?<!\S)(?:"\.{1,2}/[^"\n]+"|@\S*|\.{1,2}/\S+)')


def _git_files(root: Path) -> list[str] | None:
    """Tracked and untracked files below root, or None outside a Git checkout."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    # A file staged for deletion is still cached; listing it would complete to a
    # path that no longer exists, so drop duplicates and keep insertion order.
    return list(dict.fromkeys(result.stdout.splitlines()))


def _walked_files(root: Path) -> list[str]:
    paths: list[str] = []
    for directory, subdirectories, names in os.walk(root):
        subdirectories[:] = sorted(
            name
            for name in subdirectories
            if name not in IGNORED_DIRECTORIES and not name.startswith(".")
        )
        base = Path(directory)
        for name in sorted(names):
            if name.startswith("."):
                continue
            paths.append(str((base / name).relative_to(root)))
            if len(paths) >= MAX_FILES:
                return paths
    return paths


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


@dataclass
class WorkspaceFiles:
    """Workspace-relative file paths, refreshed at most every CACHE_SECONDS.

    Completion runs on every keystroke, so the listing is cached rather than
    re-read; new files show up within the refresh window.
    """

    root: Path
    cache_seconds: float = CACHE_SECONDS
    _paths: list[str] = field(default_factory=list)
    _loaded_at: float | None = None

    def paths(self) -> list[str]:
        now = monotonic()
        if self._loaded_at is None or now - self._loaded_at >= self.cache_seconds:
            self._paths = (_git_files(self.root) or _walked_files(self.root))[:MAX_FILES]
            self._loaded_at = now
        return self._paths

    def matches(self, fragment: str, limit: int = MAX_COMPLETIONS) -> list[str]:
        """Paths containing fragment, closest match first.

        A basename hit outranks a directory hit, and an earlier hit outranks a
        later one, so `@ui.py` offers `src/pcode/ui.py` before `tests/test_ui.py`.
        """
        needle = fragment.lstrip("@").removeprefix("./").lower()
        if not needle:
            # Shallowest first: the interesting top-level files, not build output.
            return sorted(self.paths(), key=lambda path: (path.count("/"), path))[:limit]
        ranked = []
        for path in self.paths():
            position = path.lower().find(needle)
            if position < 0:
                continue
            name_position = os.path.basename(path).lower().find(needle)
            ranked.append(((0, name_position) if name_position >= 0 else (1, position), path))
        ranked.sort(key=lambda item: (item[0], len(item[1]), item[1]))
        return [path for _, path in ranked[:limit]]

    def describe(self, path: str) -> str:
        """Size, and whether a reference to this file would carry its contents."""
        try:
            size = (self.root / path).stat().st_size
        except OSError:
            return "file"
        return f"{human_size(size)} · {'inlined' if size <= INLINE_FILE_LIMIT else 'path only'}"


def reference_fragment(text: str) -> str | None:
    """The path fragment of a trailing `@` reference, or None when absent.

    `@` only starts a reference at a word boundary, so emails and decorators in
    pasted text never open the menu.
    """
    token = text.rsplit(maxsplit=1)[-1] if text.split() else ""
    if not text or text[-1].isspace() or not token.startswith("@"):
        return None
    return token[1:]


def referenced_paths(text: str) -> list[str]:
    """Relative paths named in a prompt, in order, without repeats."""
    found = []
    for match in REFERENCE_PATTERN.finditer(text):
        token = match.group()
        if token.startswith("@"):
            continue  # An unaccepted trigger names no file yet.
        found.append(token.strip('"'))
    return list(dict.fromkeys(found))


def inline_references(text: str, workspace: Path) -> str:
    """Append the contents of short referenced files to a prompt.

    The model still sees the path it can read or edit; inlining only removes the
    first read call. Anything large, binary, or unreadable is reported by name
    and size instead, so a reference never silently means nothing.
    """
    blocks: list[str] = []
    remaining = INLINE_TOTAL_LIMIT
    for reference in referenced_paths(text):
        path = (workspace / reference).resolve()
        try:
            if not path.is_file():  # A directory or a typo speaks for itself.
                continue
            size = path.stat().st_size
            content = (
                path.read_text(encoding="utf-8")
                if size <= min(INLINE_FILE_LIMIT, remaining)
                else None
            )
        except (OSError, UnicodeDecodeError):
            continue
        if content is None:
            blocks.append(f"{reference} ({human_size(size)}): not inlined; read it if you need it.")
            continue
        remaining -= len(content)
        body = content if content.endswith("\n") else content + "\n"
        count = len(content.splitlines())
        blocks.append(
            f"=== {reference} ({count} line{'' if count == 1 else 's'}, {human_size(size)}) ===\n"
            f"{body}=== end {reference} ==="
        )
    if not blocks:
        return text
    return "\n\n".join([text, INLINE_HEADER, *blocks])


def typed_prompt(text: str) -> str:
    """A saved prompt as it was typed, without the file contents pcode appended."""
    return text.split("\n\n" + INLINE_HEADER + "\n\n", 1)[0]


class ReferenceLexer(Lexer):
    """Style file references in the editor so they read as paths, not prose."""

    def lex_document(self, document: Document):
        def line_fragments(number: int):
            line = document.lines[number]
            fragments = []
            position = 0
            for match in REFERENCE_PATTERN.finditer(line):
                fragments.append(("", line[position : match.start()]))
                fragments.append(("class:reference", match.group()))
                position = match.end()
            fragments.append(("", line[position:]))
            return fragments

        return line_fragments


class FileReferenceCompleter(Completer):
    """Replace a trailing `@fragment` with a `./` workspace-relative path."""

    def __init__(self, workspace: Path | None = None) -> None:
        self.files = WorkspaceFiles((workspace or Path.cwd()).resolve())

    def get_completions(self, document: Document, complete_event: CompleteEvent):
        fragment = reference_fragment(document.text_before_cursor)
        if fragment is None:
            return
        for path in self.files.matches(fragment):
            # The `@` is a trigger, not part of the reference: it is replaced so
            # the model receives a path its file tools accept verbatim. The
            # leading './' is what marks it as a path; only a name containing
            # whitespace needs quotes to keep its end unambiguous in prose.
            reference = f"./{path}"
            yield Completion(
                f'"{reference}" ' if any(char.isspace() for char in reference) else f"{reference} ",
                start_position=-(len(fragment) + 1),
                display=path,
                display_meta=self.files.describe(path),
            )
