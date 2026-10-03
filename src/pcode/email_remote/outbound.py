"""The messages pcode sends: always from and to the owner, replies going to the alias.

Recipients are fixed here and nowhere else: `From` and `To` are the owner,
`Reply-To` the live alias. Nothing from an incoming message's headers or from
model output can change them. Each message carries the marker header and
`Auto-Submitted`, so neither pcode nor a well-behaved client loops on it, and a
"Start new task" `mailto:` to the alias with no reply headers, which is how a
second independent session starts.
"""

from __future__ import annotations

import html
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from urllib.parse import quote

from pcode.email_remote.parsing import MARKER_HEADER

NEW_TASK_SUBJECT = "New pcode task"


def new_task_link(alias: str) -> str:
    return f"mailto:{alias}?subject={quote(NEW_TASK_SUBJECT)}"


def new_message_id(owner: str) -> str:
    return make_msgid("pcode", domain=owner.rpartition("@")[2] or None)


def reply_subject(subject: str) -> str:
    subject = subject.strip() or "pcode"
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def compose(
    *,
    owner: str,
    alias: str,
    subject: str,
    body: str,
    in_reply_to: str = "",
    references: list[str] | None = None,
    auto: str = "auto-replied",
    message_id: str = "",
) -> EmailMessage:
    """A plain-text message with a simple HTML copy; the footer is added here.

    `message_id` reuses one planned earlier (a retry); otherwise one is made.
    """
    message = EmailMessage()
    message["From"] = formataddr(("pcode", owner))
    message["To"] = owner
    message["Reply-To"] = alias
    message["Subject"] = subject
    message["Message-ID"] = message_id or new_message_id(owner)
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = " ".join([*(references or []), in_reply_to])
    message["Auto-Submitted"] = auto
    message[MARKER_HEADER] = "1"
    link = new_task_link(alias)
    footer = (
        "Reply to this email to continue. To start a separate session, "
        f"write to the address this email replies to, or open: {link}"
    )
    message.set_content(f"{body.rstrip()}\n\n--\n{footer}\n")
    message.add_alternative(
        "<html><body>"
        f'<pre style="white-space: pre-wrap; font-family: monospace">{html.escape(body)}</pre>'
        "<hr><p>Reply to this email to continue. "
        f'<a href="{html.escape(link)}">Start new task</a></p>'
        "</body></html>",
        subtype="html",
    )
    return message
