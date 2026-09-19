"""Browser-facing HTML for the loopback OAuth callback.

The loopback server answers exactly one request and then shuts down, so the page
has to be a single self-contained document: no stylesheet fetch, no fonts, no
scripts that outlive the response.

Adapted from the MIT-licensed templates in kriasoft/oauth-callback
(https://github.com/kriasoft/oauth-callback), Copyright (c) 2025-present
Konstantin Tarkus, Kriasoft.
"""

from __future__ import annotations

from html import escape

_STYLE = """
:root{color-scheme:light dark}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Ubuntu,"Helvetica Neue",sans-serif;
background:linear-gradient(180deg,#fafafa 0%,#f0f0f0 100%);color:#1a1a1a}
.card{background:#fff;border-radius:16px;padding:48px 40px 40px;max-width:420px;width:90%;
text-align:center;box-shadow:0 1px 3px rgba(0,0,0,.04),0 6px 16px rgba(0,0,0,.08);
animation:fadeUp .4s ease-out}
h1{font-size:24px;font-weight:600;margin:0 0 8px;letter-spacing:-.02em;line-height:1.2}
p{font-size:15px;color:#666;margin:0;line-height:1.5}
svg{width:56px;height:56px;margin:0 auto 24px;display:block;fill:none;stroke-width:3;
stroke-linecap:round;stroke-linejoin:round}
circle{stroke-width:2;stroke-dasharray:157;stroke-dashoffset:157;
animation:stroke .6s cubic-bezier(.65,0,.45,1) forwards}
path{stroke-dasharray:60;stroke-dashoffset:60;
animation:stroke .3s cubic-bezier(.65,0,.45,1) .4s forwards}
.ok svg{stroke:#10b981}
.bad svg{stroke:#ef4444}
@keyframes stroke{to{stroke-dashoffset:0}}
@keyframes fadeUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@media (prefers-color-scheme:dark){
body{background:linear-gradient(180deg,#0a0a0a 0%,#1a1a1a 100%);color:#fafafa}
.card{background:#1f1f1f;box-shadow:0 1px 3px rgba(0,0,0,.2),0 8px 24px rgba(0,0,0,.4)}
p{color:#999}}
@media (prefers-reduced-motion:reduce){*{animation:none!important}
circle,path{stroke-dashoffset:0}}
"""

_CHECK = '<path d="M14.1 27.2l7.1 7.2 16.7-16.8"/>'
_CROSS = '<path d="M17 17l18 18M35 17L17 35"/>'

_PAGE = (
    "<!doctype html><html lang=en><head><meta charset=utf-8>"
    '<meta name=viewport content="width=device-width, initial-scale=1">'
    "<title>{title} &middot; pcode</title><style>{style}</style></head>"
    '<body class="{kind}"><main class=card>'
    '<svg viewBox="0 0 52 52" role=img aria-hidden=true>'
    "<circle cx=26 cy=26 r=25/>{icon}</svg>"
    "<h1>{title}</h1><p>{message}</p></main></body></html>"
)


def callback_page(title: str, message: str, *, ok: bool) -> str:
    """Render the single page the browser sees after the OAuth redirect."""
    return _PAGE.format(
        title=escape(title),
        message=escape(message),
        style=_STYLE,
        kind="ok" if ok else "bad",
        icon=_CHECK if ok else _CROSS,
    )
