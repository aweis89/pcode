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
from typing import Protocol

IMAP_HOST = "imap.gmail.com"
SMTP_HOST = "smtp.gmail.com"
KEYCHAIN_SERVICE = "pcode-email"
TIMEOUT = 60


class SetupError(RuntimeError):
    """Something `pcode --email-setup` fixes; the message says what."""


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


def parse_labels(line: str) -> frozenset[str]:
    """The `X-GM-LABELS (...)` list of a FETCH response: atoms and quoted strings."""
    match = re.search(r"X-GM-LABELS \(", line)
    if match is None:
        return frozenset()
    labels, index = [], match.end()
    while index < len(line) and line[index] != ")":
        char = line[index]
        if char == " ":
            index += 1
        elif char == '"':
            index += 1
            value = []
            while index < len(line) and line[index] != '"':
                if line[index] == "\\":
                    index += 1
                value.append(line[index])
                index += 1
            labels.append("".join(value))
            index += 1
        else:
            end = index
            while end < len(line) and line[end] not in " )":
                end += 1
            labels.append(line[index:end])
            index = end
    return frozenset(labels)


def _number(name: str, line: str) -> str:
    match = re.search(rf"{name} (\d+)", line)
    if match is None:
        raise imaplib.IMAP4.error(f"FETCH response without {name}")
    return match.group(1)


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
        self._password = password
        self._imap: imaplib.IMAP4 | None = None

    def _connection(self) -> imaplib.IMAP4:
        if self._imap is None:
            imap = imaplib.IMAP4_SSL(IMAP_HOST, timeout=TIMEOUT)
            try:
                imap.login(self.owner, self._password)
                status, _ = imap.select(_all_mail(imap), readonly=True)
                if status != "OK":
                    raise imaplib.IMAP4.error("Could not open All Mail")
            except BaseException:
                imap.shutdown()
                raise
            self._imap = imap
        return self._imap

    def _uid(self, *args) -> list:
        """One UID command, reconnecting once if the connection dropped."""
        for attempt in (1, 2):
            try:
                status, data = self._connection().uid(*args)
            except (imaplib.IMAP4.abort, OSError):
                self.close()
                if attempt == 2:
                    raise
                continue
            if status != "OK":
                raise imaplib.IMAP4.error(f"UID {args[0]} failed")
            return data
        raise AssertionError("unreachable")

    def verify(self) -> None:
        """Log in and open All Mail, so setup fails now rather than at the first poll."""
        self._connection()

    def search(self, alias: str) -> list[str]:
        data = self._uid("SEARCH", "X-GM-RAW", f'"to:{alias} newer_than:1d"')
        return [uid.decode() for uid in (data[0] or b"").split()]

    def meta(self, handle: str) -> Meta:
        data = self._uid("FETCH", handle, "(X-GM-MSGID X-GM-LABELS RFC822.SIZE)")
        line = next(
            (item.decode() for item in data if isinstance(item, bytes) and b"X-GM-MSGID" in item),
            None,
        )
        if line is None:
            raise imaplib.IMAP4.error(f"No such message: {handle}")
        return Meta(
            gmail_id=_number("X-GM-MSGID", line),
            labels=parse_labels(line),
            size=int(_number("RFC822.SIZE", line)),
        )

    def _literal(self, handle: str, item: str) -> bytes:
        for part in self._uid("FETCH", handle, f"({item})"):
            if isinstance(part, tuple) and len(part) == 2:
                return part[1]
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
        except (imaplib.IMAP4.error, OSError):
            pass
