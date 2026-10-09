"""Gmail over IMAP and SMTP, and the app password in the macOS keychain.

Gmail's IMAP extensions give what acceptance needs without the Gmail API's
OAuth project: `X-GM-LABELS` carries the message-level `\\Sent` label,
`X-GM-MSGID` a stable message id, and `X-GM-RAW` Gmail's own search. The
listener reads only: the All Mail folder is opened read-only, and bodies are
fetched with `BODY.PEEK`, so nothing is marked read or relabelled.

The credential is a Gmail app password. It is read from the keychain by the
listener alone; a remote host's sandbox cannot reach the keychain, and its
environment never holds the password. There is no plaintext fallback.
"""

from __future__ import annotations

import imaplib
import re
import shutil
import smtplib
import subprocess
import sys
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol, TypeAlias

IMAP_HOST = "imap.gmail.com"
SMTP_HOST = "smtp.gmail.com"
KEYCHAIN_SERVICE = "pcode-email"
TIMEOUT = 60


class SetupError(RuntimeError):
    """Something `pcode --email-setup` fixes; the message says what."""


BAD_LOGIN = (
    "Gmail rejected the app password for {owner}. Check that it was created while "
    "signed in as {owner} (the app-passwords page opens in the browser's current "
    "Google account), that 2-Step Verification is on, and that it was copied whole; "
    "then run pcode --email-setup again."
)


def app_password(text: str) -> str:
    """Google shows app passwords as `abcd efgh ijkl mnop`; pastes also bring
    bracketed-paste markers. Neither belongs in the password."""
    return re.sub(r"\x1b\[20[01]~|\s", "", text)


@dataclass(frozen=True)
class Meta:
    """What the mailbox says about a message before its content is read."""

    gmail_id: str
    labels: frozenset[str]
    size: int

    @property
    def sent(self) -> bool:
        """The message itself was sent from the account (`\\Sent`), not delivered to it."""
        return "\\Sent" in self.labels


class Mailbox(Protocol):
    """The listener's view of the account; `GmailMailbox` is the real one."""

    def search(self, alias: str) -> list[str]: ...
    def meta(self, handle: str) -> Meta: ...
    def headers(self, handle: str) -> bytes: ...
    def fetch(self, handle: str) -> bytes: ...
    def send(self, message: EmailMessage) -> None: ...
    def close(self) -> None: ...


def _keychain() -> str:
    if sys.platform != "darwin" or (tool := shutil.which("security")) is None:
        raise SetupError(
            "The email remote keeps its app password in the macOS keychain, which "
            "this machine does not have. There is no plaintext fallback."
        )
    return tool


def read_password(owner: str) -> str:
    result = subprocess.run(
        [_keychain(), "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", owner, "-w"],
        capture_output=True,
        text=True,
    )
    if result.returncode or not result.stdout.strip():
        raise SetupError(f"No app password for {owner} in the keychain; run pcode --email-setup.")
    return result.stdout.rstrip("\n")


def store_password(owner: str) -> None:
    """Ask for the app password on the terminal (`security` prompts; pcode never sees it)."""
    subprocess.run(
        [
            _keychain(),
            "add-generic-password",
            "-U",
            "-s",
            KEYCHAIN_SERVICE,
            "-a",
            owner,
            "-l",
            "pcode email remote",
            # Trust no application: every read, the listener's included, asks
            # macOS first, so nothing else running as you reads it silently.
            "-T",
            "",
            # Last, with no value: `security` prompts for it.
            "-w",
        ],
        check=True,
    )


# Keep string values (quoted or literal bytes) distinct from protocol atoms
# (str). In particular, label contents must never become FETCH field names.
_TOKEN = re.compile(rb'\s+|[()]|"(?:[^"\\]|\\.)*"|[^\s()"{}]+')
_Value: TypeAlias = str | bytes | list["_Value"]


def _fetch_records(data: list) -> list[dict[str, _Value]]:
    """Parse imaplib's tuple/literal fragments and continuations as records."""
    records = []
    root: list[_Value] = []
    stack = [root]
    for part in data:
        if part is None:
            continue
        literal = None
        if isinstance(part, tuple) and len(part) == 2:
            fragment, literal = part
            if not isinstance(fragment, bytes) or not isinstance(literal, bytes):
                raise imaplib.IMAP4.error("Invalid FETCH literal")
            marker = re.search(rb"\{([0-9]+)\}$", fragment)
            if marker is None or int(marker[1]) != len(literal):
                raise imaplib.IMAP4.error("Invalid FETCH literal length")
            fragment = fragment[: marker.start()]
        elif isinstance(part, bytes):
            fragment = part
        else:
            raise imaplib.IMAP4.error("Invalid FETCH fragment")
        index = 0
        while index < len(fragment):
            match = _TOKEN.match(fragment, index)
            if match is None:
                raise imaplib.IMAP4.error("Invalid FETCH syntax")
            token = match[0]
            index = match.end()
            if token.isspace():
                continue
            if token == b"(":
                child: list[_Value] = []
                stack[-1].append(child)
                stack.append(child)
            elif token == b")":
                if len(stack) == 1:
                    raise imaplib.IMAP4.error("Unbalanced FETCH response")
                stack.pop()
                if len(stack) == 1:
                    if (
                        len(root) != 2
                        or not isinstance(root[0], str)
                        or not root[0].isascii()
                        or not root[0].isdigit()
                        or not isinstance(root[1], list)
                    ):
                        raise imaplib.IMAP4.error("Invalid FETCH record")
                    values = root[1]
                    if len(values) % 2:
                        raise imaplib.IMAP4.error("Unpaired FETCH field")
                    fields: dict[str, _Value] = {}
                    for key, value in zip(values[::2], values[1::2]):
                        if not isinstance(key, str) or key.upper() in fields:
                            raise imaplib.IMAP4.error("Invalid or duplicate FETCH field")
                        fields[key.upper()] = value
                    records.append(fields)
                    root.clear()
            elif token.startswith(b'"'):
                stack[-1].append(re.sub(rb"\\(.)", rb"\1", token[1:-1]))
            else:
                stack[-1].append(token.decode("ascii"))
        if literal is not None:
            if len(stack) == 1:
                raise imaplib.IMAP4.error("FETCH literal outside a record")
            stack[-1].append(literal)
    if len(stack) != 1 or root:
        raise imaplib.IMAP4.error("Incomplete FETCH response")
    return records


def _labels(fields: dict[str, _Value]) -> frozenset[str]:
    values = fields.get("X-GM-LABELS", [])
    if not isinstance(values, list):
        raise imaplib.IMAP4.error("Invalid FETCH labels")
    labels = []
    for value in values:
        if isinstance(value, list):
            raise imaplib.IMAP4.error("Invalid FETCH label")
        labels.append(value.decode() if isinstance(value, bytes) else value)
    return frozenset(labels)


def parse_labels(line: str) -> frozenset[str]:
    """The `X-GM-LABELS (...)` list of a complete FETCH response."""
    records = _fetch_records([line.encode()])
    return _labels(records[0]) if records else frozenset()


def _number(name: str, fields: dict[str, _Value]) -> str:
    value = fields.get(name)
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        raise imaplib.IMAP4.error(f"FETCH response without valid {name}")
    return value


def _message_records(data: list, handle: str) -> list[dict[str, _Value]]:
    return [fields for fields in _fetch_records(data) if fields.get("UID") == handle]


def _all_mail(imap: imaplib.IMAP4) -> str:
    """The All Mail folder, found by its `\\All` flag (its name is localized)."""
    status, lines = imap.list()
    if status != "OK":
        raise imaplib.IMAP4.error("LIST failed")
    for raw in lines:
        line = raw.decode() if isinstance(raw, bytes) else str(raw)
        match = re.match(r'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$', line)
        if match and "\\All" in match.group("flags").split():
            return match.group("name")
    raise SetupError("This account has no All Mail folder over IMAP; is it Gmail?")


class GmailMailbox:
    """One owner's Gmail: an IMAP connection kept open, and SMTP per message sent."""

    def __init__(self, owner: str, password: str) -> None:
        self.owner = owner
        self._password = app_password(password)
        self._imap: imaplib.IMAP4 | None = None

    def _connection(self) -> imaplib.IMAP4:
        if self._imap is None:
            imap = imaplib.IMAP4_SSL(IMAP_HOST, timeout=TIMEOUT)
            try:
                try:
                    imap.login(self.owner, self._password)
                except imaplib.IMAP4.error as error:
                    if "AUTHENTICATIONFAILED" in str(error):
                        raise SetupError(BAD_LOGIN.format(owner=self.owner)) from None
                    raise
                status, _ = imap.select(_all_mail(imap), readonly=True)
                if status != "OK":
                    raise imaplib.IMAP4.error("Could not open All Mail")
            except BaseException:
                imap.shutdown()
                raise
            self._imap = imap
        return self._imap

    def _command(self, name: str, *args) -> list:
        """One IMAP command, reconnecting once if the connection dropped."""
        for attempt in (1, 2):
            try:
                status, data = getattr(self._connection(), name)(*args)
            except imaplib.IMAP4.abort, OSError:
                self.close()
                if attempt == 2:
                    raise
                continue
            if status != "OK":
                raise imaplib.IMAP4.error(f"{name.upper()} {args[0] if args else ''} failed")
            return data
        raise AssertionError("unreachable")

    def _uid(self, *args) -> list:
        return self._command("uid", *args)

    def verify(self) -> None:
        """Log in and open All Mail, so setup fails now rather than at the first poll."""
        self._connection()

    def search(self, alias: str) -> list[str]:
        # A selected mailbox is a snapshot: Gmail only adds mail that arrived
        # since SELECT once the client polls, so without NOOP a long-lived
        # connection searches the mailbox as it was at login.
        self._command("noop")
        data = self._uid("SEARCH", "X-GM-RAW", f'"to:{alias} newer_than:1d"')
        return [uid.decode() for uid in (data[0] or b"").split()]

    def meta(self, handle: str) -> Meta:
        data = self._uid("FETCH", handle, "(X-GM-MSGID X-GM-LABELS RFC822.SIZE)")
        for fields in _message_records(data, handle):
            # An unsolicited FLAGS update for this UID is not our result.
            if not {"X-GM-MSGID", "X-GM-LABELS", "RFC822.SIZE"} <= fields.keys():
                continue
            return Meta(
                gmail_id=_number("X-GM-MSGID", fields),
                labels=_labels(fields),
                size=int(_number("RFC822.SIZE", fields)),
            )
        raise imaplib.IMAP4.error(f"No metadata for message {handle}")

    def _literal(self, handle: str, item: str) -> bytes:
        data = self._uid("FETCH", handle, f"({item})")
        # PEEK is a request modifier, not part of the returned BODY field.
        field = item.replace("BODY.PEEK[", "BODY[")
        for fields in _message_records(data, handle):
            value = fields.get(field)
            if isinstance(value, bytes):
                return value
        raise imaplib.IMAP4.error(f"No content for message {handle}")

    def headers(self, handle: str) -> bytes:
        return self._literal(handle, "BODY.PEEK[HEADER]")

    def fetch(self, handle: str) -> bytes:
        return self._literal(handle, "BODY.PEEK[]")

    def send(self, message: EmailMessage) -> None:
        with smtplib.SMTP_SSL(SMTP_HOST, 465, timeout=TIMEOUT) as smtp:
            smtp.login(self.owner, self._password)
            # Only ever to the owner: never a recipient read from mail or a model.
            smtp.send_message(message, from_addr=self.owner, to_addrs=[self.owner])

    def close(self) -> None:
        imap, self._imap = self._imap, None
        if imap is None:
            return
        try:
            imap.logout()
        except imaplib.IMAP4.error, OSError:
            pass
