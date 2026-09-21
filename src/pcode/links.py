"""Collect URLs from a conversation and open them in the user's browser."""

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

# Markdown [label](url) first so the label survives; then bare URLs. Trailing
# punctuation that prose attaches to a URL is not part of it.
MARKDOWN_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
BARE_URL = re.compile(r"https?://[^\s<>\"'`\]]+")
_TRAILING = ".,;:!?'\""


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


def extract_links(text: str, source: str = "") -> list[Link]:
    """Return links in first-seen order, one per distinct URL."""
    found: dict[str, Link] = {}
    for match in MARKDOWN_LINK.finditer(text):
        url = _strip(match.group(2))
        found.setdefault(url, Link(url, match.group(1).strip(), source))
    remainder = MARKDOWN_LINK.sub(" ", text)
    for match in BARE_URL.finditer(remainder):
        url = _strip(match.group(0))
        found.setdefault(url, Link(url, "", source))
    return list(found.values())


def conversation_links(tree) -> list[Link]:
    """Links from prompts, tools, and responses on the active path, oldest first."""
    found: dict[str, Link] = {}
    for identity in tree.path(tree.active):
        node = tree.nodes[identity]
        if node.kind != "turn":
            continue
        for links in (
            extract_links(node.prompt, "user"),
            node.tool_links.values(),
            extract_links(node.response, "assistant"),
        ):
            for link in links:
                found.setdefault(link.url, link)
    return list(found.values())


def open_link(url: str) -> None:
    """Hand the URL to the desktop without tying it to this terminal.

    ``webbrowser`` is avoided on purpose: without a display it falls back to
    text browsers such as lynx and takes over the terminal pcode is drawing.
    """
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
