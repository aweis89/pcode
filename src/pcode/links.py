"""Collect URLs from a conversation and open them in the editor or browser."""

import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

# Markdown [label](url) first so the label survives; then bare URLs. Trailing
# punctuation that prose attaches to a URL is not part of it.
MARKDOWN_LINK = re.compile(r"\[([^\]\n]+)\]\(((?:https?|file)://[^\s)\\]+)\)", re.IGNORECASE)
BARE_URL = re.compile(r"(?:https?|file)://[^\s<>\"'`\]\\]+", re.IGNORECASE)
_TRAILING = ".,;:!?'\""
_LINE_FRAGMENT = re.compile(
    r"^(?:L(\d+)(?:(?:C\d+)?(?:-(?:L?\d+(?:C\d+)?))?|:\d+)?|line=(\d+)|(\d+))$",
    re.IGNORECASE,
)
_COLON_LINE_COL = re.compile(r"^(.*):(\d+):\d+$")
_COLON_LINE = re.compile(r"^(.*):(\d+)$")
_WINDOWS_DRIVE = re.compile(r"^/[A-Za-z]:")
# Extensions whose MIME type is a media format (e.g. MPEG-TS video/mp2t) even
# though they are commonly source code; let content sniffing decide for them.
_SNIFF_TEXT_EXTENSIONS = frozenset({".ts", ".mts", ".cts"})
_NON_TEXT_MIME_PREFIXES = ("image/", "audio/", "video/", "font/")
_NON_TEXT_MIME_TYPES = frozenset(
    {
        "application/pdf",
        "application/zip",
        "application/gzip",
        "application/x-gzip",
        "application/x-tar",
        "application/x-bzip2",
        "application/x-xz",
        "application/x-7z-compressed",
        "application/x-rar-compressed",
        "application/octet-stream",
        "application/wasm",
        "application/msword",
    }
)
_TEXT_CONTROL_BYTES = frozenset({7, 8, 9, 10, 12, 13, 27})


@dataclass(frozen=True)
class Link:
    url: str
    label: str = ""
    source: str = ""  # "user", "assistant", or a tool name


def _strip(url: str) -> str:
    url = url.rstrip(_TRAILING)
    # Balanced parentheses as in Wikipedia URLs stay; an unmatched close goes.
    while url.endswith(")") and url.count("(") < url.count(")"):
        url = url[:-1].rstrip(_TRAILING)
    return url


def remember_link(found: dict[str, Link], link: Link) -> None:
    """Move repeated URLs to their latest position, retaining useful labels."""
    previous = found.pop(link.url, None)
    label = link.label or (previous.label if previous else "")
    found[link.url] = Link(link.url, label, link.source)


def extract_links(text: str, source: str = "") -> list[Link]:
    """Return distinct URLs in last-seen order, oldest first."""
    matches = [
        (match.start(), Link(_strip(match.group(2)), match.group(1).strip(), source))
        for match in MARKDOWN_LINK.finditer(text)
    ]
    # Preserve offsets while hiding Markdown links from the bare-URL pass.
    remainder = MARKDOWN_LINK.sub(lambda match: " " * len(match.group(0)), text)
    matches.extend(
        (match.start(), Link(_strip(match.group(0)), "", source))
        for match in BARE_URL.finditer(remainder)
    )
    found: dict[str, Link] = {}
    for _, link in sorted(matches, key=lambda item: item[0]):
        remember_link(found, link)
    return list(found.values())


def conversation_links(tree, *, include_tools: bool = True) -> list[Link]:
    """Links on the active path, oldest first; filter before deduplicating URLs."""
    found: dict[str, Link] = {}
    for identity in tree.path(tree.active):
        node = tree.nodes[identity]
        # A compaction's summary restates links it already listed; a merged
        # side answer is part of the conversation like any reply.
        if node.kind == "compaction":
            continue
        links = node.links if include_tools else node.message_links
        for link in links.values():
            remember_link(found, link)
    return list(found.values())


def editor_command(path: Path, line: int | None, environ=os.environ) -> list[str]:
    """$VISUAL or $EDITOR opening `path`, at `line` where the editor can be told it."""
    raw = (environ.get("VISUAL") or environ.get("EDITOR") or "vi").strip()
    try:
        args = shlex.split(raw)
    except ValueError as error:
        raise RuntimeError(f"Invalid editor command {raw!r}: {error}") from error
    if not args:
        args = ["vi"]
    name = Path(args[0]).name
    if not line:
        return [*args, str(path)]
    if name in {"code", "code-insiders", "codium", "cursor", "windsurf"}:
        return [*args, "--goto", f"{path}:{line}"]
    if name in {"subl", "zed", "hx", "helix"}:
        return [*args, f"{path}:{line}"]
    return [*args, f"+{line}", str(path)]


def file_target(url: str) -> tuple[Path, int | None] | None:
    """Decode a local ``file://`` URL into ``(path, line)``, or None for other URLs."""
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "file":
        return None
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        return None
    raw_path = unquote(parsed.path)
    if sys.platform == "win32" and _WINDOWS_DRIVE.match(raw_path):
        raw_path = raw_path[1:]
    if not raw_path:
        return None
    line: int | None = None
    if parsed.fragment and (match := _LINE_FRAGMENT.match(parsed.fragment)):
        number = int(next(group for group in match.groups() if group is not None))
        if number >= 1:
            line = number
    path = Path(raw_path)
    if line is None and not path.exists():
        for pattern in (_COLON_LINE_COL, _COLON_LINE):
            if colon := pattern.match(raw_path):
                candidate = Path(colon.group(1))
                if candidate.exists():
                    path = candidate
                    number = int(colon.group(2))
                    if number >= 1:
                        line = number
                    break
    return path, line


def is_text_file(path: Path) -> bool:
    """Whether an existing path looks like a text file suitable for ``$EDITOR``."""
    if not path.is_file():
        return False
    mime, encoding = mimetypes.guess_type(path.name)
    if encoding is not None:
        return False
    if mime and path.suffix.lower() not in _SNIFF_TEXT_EXTENSIONS:
        if mime.startswith(_NON_TEXT_MIME_PREFIXES) or mime in _NON_TEXT_MIME_TYPES:
            return False
        if mime.startswith(("application/vnd.ms-", "application/vnd.openxmlformats-")):
            return False
    try:
        with path.open("rb") as handle:
            chunk = handle.read(8192)
    except OSError:
        return False
    if not chunk:
        return True
    if b"\x00" in chunk:
        return False
    control = sum(1 for byte in chunk if byte < 32 and byte not in _TEXT_CONTROL_BYTES)
    if control / len(chunk) >= 0.05:
        return False
    # A valid UTF-8 prefix (allowing a split multibyte sequence at the 8 KiB boundary) is text.
    for trim in range(min(4, len(chunk))):
        try:
            (chunk[:-trim] if trim else chunk).decode("utf-8")
            return True
        except UnicodeDecodeError:
            if len(chunk) < 8192:
                break
    non_ascii = sum(1 for byte in chunk if byte >= 127)
    return (control + non_ascii) / len(chunk) < 0.3


def link_editor_command(url: str, environ=os.environ) -> list[str] | None:
    """Editor command for a local text ``file://`` link, or None to use the OS opener.

    Raises ``RuntimeError`` when a local ``file://`` target does not exist.
    """
    target = file_target(url)
    if target is None:
        return None
    path, line = target
    if not path.exists():
        raise RuntimeError(f"File does not exist: {path}")
    if not is_text_file(path):
        return None
    return editor_command(path, line, environ=environ)


def open_link(url: str) -> None:
    """Hand the URL to the desktop without tying it to this terminal.

    ``webbrowser`` is avoided on purpose: without a display it falls back to
    text browsers such as lynx and takes over the terminal pcode is drawing.
    """
    target = file_target(url)
    if target is not None:
        path, _ = target
        if not path.exists():
            raise RuntimeError(f"File does not exist: {path}")
        url = str(path)
    if sys.platform == "win32":
        os.startfile(url)  # type: ignore[attr-defined]
        return
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    if shutil.which(opener) is None:
        raise RuntimeError(f"Cannot open links: `{opener}` is not on PATH.")
    subprocess.Popen(
        [opener, url],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
