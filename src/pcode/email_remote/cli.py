"""`pcode --email-setup` and `pcode --email-listen`."""

from __future__ import annotations

import asyncio
import re
import signal
import sys
import time
from pathlib import Path

from pcode.email_remote.listener import Limits, Listener, head_commit
from pcode.email_remote.mailbox import GmailMailbox, SetupError, read_password, store_password
from pcode.email_remote.state import state_dir
from pcode.preferences import load_preferences, update_preferences
from pcode.remote_profile import RemoteProfile

_DURATION = re.compile(r"^(\d+(?:\.\d+)?)([smhd]?)$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "": 60}
_ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
APP_PASSWORDS = "https://myaccount.google.com/apppasswords"


def parse_duration(text: str) -> float:
    """`8h`, `30m`, `1d`, `90s`; a bare number is minutes."""
    match = _DURATION.match(text.strip().lower())
    if match is None or float(match.group(1)) <= 0:
        raise ValueError(f"Not a duration: {text!r} (use e.g. 30m, 8h, 1d).")
    return float(match.group(1)) * _UNITS[match.group(2)]


def profile_from_preferences(preferences: dict, base: str | None = None) -> RemoteProfile:
    """The profile email-started hosts run under: chosen here, never by email."""
    return RemoteProfile(
        # The listener's state (routes, the outbox, replies); the keychain is denied anyway.
        deny_read=(str(state_dir()),),
        mcp=preferences.get("email_mcp", "off") == "on",
        turn_minutes=float(preferences.get("email_turn_minutes") or 30),
        max_requests=int(preferences.get("email_turn_requests") or 100),
        max_tool_calls=int(preferences.get("email_turn_tool_calls") or 100),
        base=base,
        origin="email",
    )


def setup(owner: str | None, ask=input) -> int:
    """Store the app password in the keychain, check it, and confirm the profile once."""
    owner = (owner or ask("Gmail address: ")).strip().lower()
    if not _ADDRESS.match(owner):
        raise SetupError(f"{owner!r} is not an email address.")
    print(
        "pcode reads and sends this account's mail over IMAP with a Gmail app password "
        f"(2-Step Verification required): create one at {APP_PASSWORDS}.\n"
        "Paste it at the keychain prompt below; it goes straight into the macOS "
        "keychain, and only the listener reads it.",
        file=sys.stderr,
    )
    store_password(owner)
    mailbox = GmailMailbox(owner, read_password(owner))
    try:
        mailbox.verify()
    finally:
        mailbox.close()
    print("Signed in to Gmail over IMAP.\n", file=sys.stderr)
    print("Email-started sessions run unattended, under this profile:", file=sys.stderr)
    for line in profile_from_preferences(load_preferences()).describe():
        print(f"  {line}", file=sys.stderr)
    answer = ask("Let email from this account start sessions under it? [y/N] ")
    if answer.strip().lower() not in ("y", "yes"):
        print("Not enabled.", file=sys.stderr)
        return 1
    update_preferences({"email_owner": owner})
    print(f"Enabled for {owner}. Start listening with: pcode --email-listen", file=sys.stderr)
    return 0


def listen(workspace: Path, *, ttl: str, model: str | None) -> int:
    from pcode.worktree import main_checkout

    preferences = load_preferences()
    owner = preferences.get("email_owner")
    if not owner:
        raise SetupError("No email account is set up; run pcode --email-setup.")
    model = model or preferences.get("model")
    if not model:
        raise SetupError("Email sessions need a model: pass -m or set a default with /model.")
    workspace = workspace.resolve()
    if main_checkout(workspace) is None:
        raise SetupError(f"{workspace} is not in a git repository; email sessions need worktrees.")
    seconds = parse_duration(ttl)
    mailbox = GmailMailbox(owner, read_password(owner))
    mailbox.verify()
    profile = profile_from_preferences(preferences, base=head_commit(workspace))
    listener = Listener(
        owner=owner,
        mailbox=mailbox,
        workspace=workspace,
        profile=profile,
        ttl=seconds,
        state_path=state_dir() / "state.json",
        limits=Limits.from_preferences(preferences),
        model=model,
    )
    expiry = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(listener.expires))
    print(
        "\n".join(
            [
                f"Listening for email from {owner}",
                f"Workspace: {workspace} (model {model})",
                "Profile:",
                *(f"  {line}" for line in profile.describe()),
                f"Until: {expiry}. Ctrl+C stops listening and every email session.",
            ]
        ),
        file=sys.stderr,
        flush=True,
    )

    async def main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
        try:
            await listener.start()
            await listener.run(stop)
        finally:
            mailbox.close()

    asyncio.run(main())
    return 0
