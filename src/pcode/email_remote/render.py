"""Markdown replies as email HTML that Gmail (web and app) shows as written.

The model writes Markdown; the plain-text part keeps it as is and the HTML part
is rendered here. Gmail drops or rewrites `<style>` blocks unpredictably, so
every style is an attribute added to the tag at render time, from the fixed
table below, never from the model's text.

Model output is untrusted: raw HTML is escaped (`html: False`), links keep
markdown-it's scheme check (no `javascript:` or `data:`), and images become
plain links, since an `<img>` loads on open and a model-chosen URL would be a
way to send data out the moment the email is read.
"""

from __future__ import annotations

import html
import re

from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml
from pygments import highlight as pygmentize
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"
CODE_BOX = (
    f"font-family:{MONO};font-size:13px;line-height:1.45;background:#f6f8fa;"
    "border:1px solid #d0d7de;border-radius:6px;padding:10px 12px;margin:0 0 12px;"
    "white-space:pre-wrap;word-wrap:break-word"
)
STYLES = {
    "p": "margin:0 0 12px",
    "h1": "font-size:20px;margin:16px 0 8px",
    "h2": "font-size:18px;margin:16px 0 8px",
    "h3": "font-size:16px;margin:14px 0 6px",
    "h4": "font-size:14px;margin:12px 0 6px",
    "h5": "font-size:14px;margin:12px 0 6px",
    "h6": "font-size:14px;margin:12px 0 6px;color:#57606a",
    "ul": "margin:0 0 12px;padding-left:24px",
    "ol": "margin:0 0 12px;padding-left:24px",
    "li": "margin:2px 0",
    "blockquote": "margin:0 0 12px;padding:0 12px;border-left:4px solid #d0d7de;color:#57606a",
    "table": "border-collapse:collapse;margin:0 0 12px",
    "th": "border:1px solid #d0d7de;padding:4px 10px;background:#f6f8fa;text-align:left",
    "td": "border:1px solid #d0d7de;padding:4px 10px",
    "code": (
        f"font-family:{MONO};font-size:85%;background:#eff1f3;border-radius:4px;padding:1px 4px"
    ),
    "a": "color:#0969da",
    "hr": "border:0;border-top:1px solid #d0d7de;margin:16px 0",
}
# Every tag markdown-it emits, with whatever attributes it gave it. With raw
# HTML off, a `<` in the output is always markdown-it's own, never the model's.
_TAG = re.compile(r"<(%s)((?:\s[^>]*?)?)\s*(/?)>" % "|".join(STYLES))
_STYLE_ATTR = re.compile(r'\sstyle="([^"]*)"')


def _style_tag(match: re.Match) -> str:
    name, attrs, close = match.groups()
    style = STYLES[name]
    existing = _STYLE_ATTR.search(attrs)
    if existing:  # Table alignment: it replaces the default one.
        style = f"{re.sub(r';?text-align:[a-z]+', '', style)};{existing.group(1)}"
        attrs = _STYLE_ATTR.sub("", attrs)
    return f'<{name}{attrs} style="{style}"{" /" if close else ""}>'


# Pygments' inline styles make code about 8x larger, and Gmail clips a message
# whose HTML passes ~102 KB, hiding the session detail and footer at its end.
# So long blocks stay plain, and so does everything if the total is still large.
HIGHLIGHT_LIMIT = 8_000
HTML_BUDGET = 90_000


def _code_block(code: str, language: str, env: dict) -> str:
    lexer = None
    if language and env.get("highlight", True) and len(code) <= HIGHLIGHT_LIMIT:
        try:
            lexer = get_lexer_by_name(language)
        except ClassNotFound:
            pass
    if lexer is None:
        inner = escapeHtml(code)
    else:
        formatter = HtmlFormatter(noclasses=True, nowrap=True, style="default")
        inner = pygmentize(code, lexer, formatter)
    # Built here, after styling, so the `<code>` inside keeps no inline-code chip.
    return f'<pre style="{CODE_BOX}">{inner.rstrip()}</pre>\n'


def _markdown() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
    md.enable(["table", "strikethrough"])

    def fence(renderer, tokens, idx, options, env):
        token = tokens[idx]
        language = token.info.strip().split(maxsplit=1)[0] if token.info.strip() else ""
        return _code_block(token.content, language, env)

    def code_block(renderer, tokens, idx, options, env):
        return _code_block(tokens[idx].content, "", env)

    # Only web links are live. A relative path (`[app.py](src/app.py)`, common
    # from a coding agent) or a mailto: cannot work from an email: plain text.
    def link_open(renderer, tokens, idx, options, env):
        href = str(tokens[idx].attrGet("href") or "")
        live = href.lower().startswith(("https://", "http://"))
        env.setdefault("links", []).append(live)
        return renderer.renderToken(tokens, idx, options, env) if live else ""

    def link_close(renderer, tokens, idx, options, env):
        live = env["links"].pop()
        return renderer.renderToken(tokens, idx, options, env) if live else ""

    def image(renderer, tokens, idx, options, env):
        token = tokens[idx]
        src = str(token.attrGet("src") or "")
        alt = token.content or "image"
        # markdown-it's own check still allows data:image/...; only web links here.
        if not src.lower().startswith(("https://", "http://")):
            return escapeHtml(alt)
        return f'<a href="{escapeHtml(src)}">[image: {escapeHtml(alt)}]</a>'

    md.add_render_rule("fence", fence)
    md.add_render_rule("code_block", code_block)
    md.add_render_rule("image", image)
    md.add_render_rule("link_open", link_open)
    md.add_render_rule("link_close", link_close)
    return md


_MD = _markdown()
# Code blocks are rendered whole by _code_block; mark them so styling skips them.
_PRE = re.compile(r'<pre style="[^"]*">.*?</pre>', re.S)


def markdown_html(text: str, *, highlight: bool = True) -> str:
    """Render Markdown to styled HTML fragments (no `<html>` wrapper)."""
    rendered = _MD.render(text, {"highlight": highlight})
    blocks: list[str] = []

    def stash(match: re.Match) -> str:
        blocks.append(match.group(0))
        return f"\x00{len(blocks) - 1}\x00"

    rendered = _PRE.sub(stash, rendered)
    rendered = _TAG.sub(_style_tag, rendered)
    return re.sub(r"\x00(\d+)\x00", lambda m: blocks[int(m.group(1))], rendered)


def document(body: str, trailer: str, link: str) -> str:
    """The whole HTML part: rendered reply, grey trailer, and the footer actions."""
    rendered = markdown_html(body)
    if len(rendered) > HTML_BUDGET:
        rendered = markdown_html(body, highlight=False)
    parts = [rendered]
    if trailer.strip():
        parts.append(
            f'<pre style="font-family:{MONO};font-size:12px;line-height:1.4;color:#57606a;'
            "white-space:pre-wrap;word-wrap:break-word;margin:16px 0 0;padding-top:10px;"
            f'border-top:1px solid #d0d7de">{html.escape(trailer.strip())}</pre>'
        )
    parts.append(
        f'<p style="margin:16px 0 0;font-size:13px;color:#57606a">Reply to this email to '
        f'continue. &nbsp;<a href="{html.escape(link)}" style="display:inline-block;'
        "padding:5px 12px;border:1px solid #d0d7de;border-radius:6px;background:#f6f8fa;"
        'color:#24292f;text-decoration:none;font-weight:600">Start new task</a></p>'
    )
    return (
        '<html><body style="margin:0;padding:0">'
        f'<div style="max-width:720px;font-family:{SANS};font-size:14px;line-height:1.5;'
        f'color:#1f2328">{"".join(parts)}</div></body></html>'
    )
