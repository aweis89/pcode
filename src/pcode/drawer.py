"""Experimental: /btw threads in a native drawer beside the terminal (`btw_view drawer`).

Ghostty Smooth (the pcode fork of Ghostty) can open a WebKit drawer next to a
terminal pane. pcode asks for it with one short APC, and the drawer reads the
thread straight from the session host's socket, so the escape sequence names
what to show and never carries it:

    ESC _ pcode;?  ESC \\                    probe; the terminal answers `pcode;ok;1`
    ESC _ pcode;btw;<host>;<thread> ESC \\   show this host's /btw thread
    ESC _ pcode;close ESC \\                 close the drawer

The drawer connects to `<host_dir>/<host>.sock` as an observer (`SessionHost`'s
`observe` hello): it does not count as a terminal and may call nothing. It is
sent the side questions with each answer already rendered here as HTML, so the
page needs no Markdown library and model text can never become markup: raw
HTML is escaped, and only web links stay live.

The editor keeps replying to the thread as with `btw_view swap`; only drawing
it moves out of the terminal.
"""

import base64
import hashlib
import os
import re
from functools import cache
from pathlib import Path

from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml

PROTOCOL = 1
QUERY = b"\x1b_pcode;?\x1b\\"
# Any pcode reply, so a future shape is still kept out of typing.
REPLY = re.compile(rb"\x1b_pcode;([^\x1b\x07]{0,32})\x1b\\")
VERSION = re.compile(rb"ok;(\d{1,4})")
CLOSE = "\x1b_pcode;close\x1b\\"
# What the drawer's hello sends: it speaks this, not the terminals' `PROTOCOL`.
OBSERVER_VERSION = 1


# What the terminal accepts as a host id and a thread (it checks again).
_ID = re.compile(r"[0-9a-f]{8}")
_THREAD = re.compile(r"[A-Za-z0-9_-]{1,64}")


def valid_host(host: str) -> bool:
    return _ID.fullmatch(host) is not None


def open_btw(host: str, thread: str) -> str:
    if not (_ID.fullmatch(host) and _THREAD.fullmatch(thread)):
        raise ValueError(f"Not a drawer host and thread: {host!r}, {thread!r}")
    return f"\x1b_pcode;btw;{host};{thread}\x1b\\"


def default_host_dir() -> Path:
    """Where the terminal looks for host sockets: it cannot see this process's environment."""
    return Path.home() / ".local" / "state" / "pcode" / "hosts"


@cache
def reachable(directory: Path) -> bool:
    """Whether the terminal app can find hosts in `directory`: local, and the default place.

    Cached: asked every frame, and neither answer changes while pcode runs.
    """
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    try:
        return directory.resolve() == default_host_dir().resolve()
    except OSError:
        return False


_SCRIPT = re.compile(r"<script>(.*?)</script>", re.S)


@cache
def page() -> str:
    """The drawer's page, its one inline script allowed by hash and no other."""
    text = Path(__file__).with_name("drawer.html").read_text(encoding="utf-8")
    script = _SCRIPT.search(text)
    assert script is not None, "drawer.html has its script inline"
    digest = base64.b64encode(hashlib.sha256(script[1].encode()).digest()).decode()
    return text.replace("'unsafe-inline-script'", f"'sha256-{digest}'")


def _markdown() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
    md.enable(["table", "strikethrough"])

    # Only web links are live: a relative path or file: link means nothing in the page.
    def link_open(renderer, tokens, idx, options, env):
        href = str(tokens[idx].attrGet("href") or "")
        live = href.lower().startswith(("https://", "http://"))
        env.setdefault("links", []).append(live)
        return renderer.renderToken(tokens, idx, options, env) if live else ""

    def link_close(renderer, tokens, idx, options, env):
        live = env["links"].pop()
        return renderer.renderToken(tokens, idx, options, env) if live else ""

    # An image would load when shown, sending a model-chosen URL a request: a link instead.
    def image(renderer, tokens, idx, options, env):
        token = tokens[idx]
        src = str(token.attrGet("src") or "")
        alt = escapeHtml(token.content or "image")
        if not src.lower().startswith(("https://", "http://")):
            return alt
        return f'<a href="{escapeHtml(src)}">[image: {alt}]</a>'

    md.add_render_rule("link_open", link_open)
    md.add_render_rule("link_close", link_close)
    md.add_render_rule("image", image)
    return md


_MD = _markdown()


def markdown_html(text: str) -> str:
    """`text` as HTML fragments; model text never becomes a tag of its own."""
    return _MD.render(text, {})


def observer_state(state: dict) -> dict:
    """A side question's state (`SessionHost.aside_state`) as the drawer shows it."""
    from pcode.terminal_text import safe_text

    # The page shows the rendered answer only, so the raw one (and what it
    # continues from) is not sent twice the size.
    shown = {k: v for k, v in state.items() if k not in ("answer", "conversation", "base")}
    return {**shown, "answer_html": markdown_html(safe_text(state.get("answer") or ""))}
