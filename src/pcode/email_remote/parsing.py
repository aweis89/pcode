"""One raw email, parsed: the headers acceptance needs, and the new text of its body.

Only the standard library's parser reads the message. Headers are taken as
parsed, never from the body. The body's new text is what the owner wrote above
Gmail's quoted copy of the thread; anything doubtful (no text part, quoted
text only, too long) yields a reason instead of text, and the listener asks
for a clean resend rather than guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses
from html.parser import HTMLParser

# The new text a message may carry, and the whole message pcode will fetch.
MAX_TEXT_BYTES = 64 * 1024
MAX_MESSAGE_BYTES = 2 * 1024 * 1024

# Marks every message pcode sends, so it never takes one of its own as input.
MARKER_HEADER = "X-Pcode-Remote"

# Why a message has no usable text; the listener answers each with a resend request.
EMPTY, MALFORMED, TOO_LONG = "empty", "malformed", "too-long"

_LIST_HEADERS = ("List-Id", "List-Unsubscribe", "List-Post", "Mailing-List")
_BULK = {"bulk", "list", "junk"}
# A Gmail attribution: "On Mon, 1 Jan 2026 at 10:00, Name <a@b.c> wrote:", which
# Gmail wraps onto two lines when long.
_ATTRIBUTION = re.compile(r"^On .+wrote:\s*$", re.DOTALL)
# Lines mobile clients add, dropped only as the very last line of the new text.
_SENT_FROM = re.compile(r"^(Sent from my \w+|Sent from Gmail Mobile|Get Outlook for \w+)$")


@dataclass
class Incoming:
    """What the listener reads from one message."""

    message_id: str
    subject: str
    to: list[str]
    senders: list[str]
    # Ancestry: References, then In-Reply-To, without repeats.
    ancestry: list[str]
    marker: bool
    auto_submitted: str
    resent: bool
    sender_header: list[str]
    list_mail: bool
    report: bool
    # The new text, or "" with `problem` saying why there is none.
    text: str = ""
    problem: str = ""
    attachments: list[str] = field(default_factory=list)


def parse(raw: bytes) -> Incoming:
    """The headers of `raw`; the body is left for `read_body`, once accepted."""
    message = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    senders = _addresses(message, "From")
    if len(message.get_all("From") or []) != 1:
        # Two From headers are as conflicting as two mailboxes in one.
        senders.append("(conflicting)")
    content_type = message.get_content_type()
    return Incoming(
        message_id=str(message.get("Message-ID", "") or "").strip(),
        subject=" ".join(str(message.get("Subject", "") or "").split()),
        to=_addresses(message, "To"),
        senders=senders,
        ancestry=_ancestry(message),
        marker=MARKER_HEADER in message,
        auto_submitted=str(message.get("Auto-Submitted", "") or "").strip().lower(),
        resent=any(key.lower().startswith("resent-") for key in message.keys()),
        sender_header=_addresses(message, "Sender"),
        list_mail=any(key in message for key in _LIST_HEADERS)
        or str(message.get("Precedence", "")).strip().lower() in _BULK,
        report=content_type in ("multipart/report", "message/delivery-status"),
    )


def read_body(raw: bytes, incoming: Incoming) -> Incoming:
    """Fill in the new text (or why there is none) and the attachment names."""
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
        text, attachments = _body(message)
    except LookupError, ValueError, UnicodeError, AttributeError:
        incoming.problem = MALFORMED
        return incoming
    incoming.attachments = attachments
    if text is None:
        incoming.problem = EMPTY if attachments else MALFORMED
        return incoming
    new = new_text(text)
    if not new:
        incoming.problem = EMPTY
    elif len(new.encode()) > MAX_TEXT_BYTES:
        incoming.problem = TOO_LONG
    else:
        incoming.text = new
    return incoming


def _addresses(message: EmailMessage, name: str) -> list[str]:
    values = [str(value) for value in message.get_all(name) or []]
    return [address.strip().lower() for _, address in getaddresses(values) if address.strip()]


def _ancestry(message: EmailMessage) -> list[str]:
    ids: list[str] = []
    for name in ("References", "In-Reply-To"):
        for value in message.get_all(name) or []:
            for found in re.findall(r"<[^<>\s]+>", str(value)):
                if found not in ids:
                    ids.append(found)
    return ids


def _body(message: EmailMessage) -> tuple[str | None, list[str]]:
    """The best text part (plain preferred, else HTML as text), and attachment names."""
    attachments = []
    plain = rich = None
    for part in message.walk():
        if part.is_multipart():
            continue
        kind = part.get_content_type()
        if part.get_content_disposition() == "attachment" or kind not in (
            "text/plain",
            "text/html",
        ):
            attachments.append(part.get_filename() or kind)
        elif kind == "text/plain" and plain is None:
            plain = part
        elif kind == "text/html" and rich is None:
            rich = part
    if plain is not None:
        return str(plain.get_content()), attachments
    if rich is not None:
        return html_text(str(rich.get_content())), attachments
    return None, attachments


class _TextExtractor(HTMLParser):
    """Visible text with line breaks, minus Gmail's quoted thread and anything active."""

    BLOCKS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre"}
    SKIPPED = {"script", "style", "head", "title", "blockquote"}
    VOID = {"br", "img", "hr", "meta", "link", "input", "wbr", "col", "source"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        # Open elements, and the depth at which a skipped subtree began.
        self.stack: list[str] = []
        self.skip_from: int | None = None

    def handle_starttag(self, tag, attrs):
        classes = (dict(attrs).get("class") or "").split()
        if self.skip_from is None and (
            tag in self.SKIPPED or "gmail_quote" in classes or "gmail_signature" in classes
        ):
            self.skip_from = len(self.stack)
        if tag in self.BLOCKS and self.skip_from is None:
            self.parts.append("\n")
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag not in self.stack:
            return
        while self.stack and self.stack.pop() != tag:
            pass
        if self.skip_from is not None and len(self.stack) <= self.skip_from:
            self.skip_from = None
            return
        if tag in self.BLOCKS and self.skip_from is None:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.skip_from is None:
            self.parts.append(data)


def html_text(markup: str) -> str:
    """Text from HTML, fetching nothing and dropping the quoted thread."""
    extractor = _TextExtractor()
    extractor.feed(markup)
    extractor.close()
    text = "".join(extractor.parts).replace("\xa0", " ")
    lines = [line.rstrip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def new_text(text: str) -> str:
    """What the owner wrote: above Gmail's attribution and quote, above a `-- ` signature.

    Conservative: a quote is cut only where an attribution line ("On ... wrote:")
    is followed by `>` lines, so quoted code or a `>` the owner typed stays. A
    signature is cut only at the standard `-- ` delimiter, not at any `--` line.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = _before_quote(lines)
    if "-- " in lines:
        lines = lines[: lines.index("-- ")]
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and _SENT_FROM.match(lines[-1].strip()):
        lines.pop()
    return "\n".join(lines).strip()


def _before_quote(lines: list[str]) -> list[str]:
    for index, line in enumerate(lines):
        if not line.startswith("On "):
            continue
        # The attribution may wrap onto the next line.
        for span in (1, 2):
            if not _ATTRIBUTION.match("\n".join(lines[index : index + span])):
                continue
            rest = [later for later in lines[index + span :] if later.strip()]
            if rest and rest[0].startswith(">"):
                return lines[:index]
    return lines
