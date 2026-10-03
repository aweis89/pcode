"""Phase 1 of dev/email-remote.md: check the Gmail assumptions on a real account.

Throwaway evidence-gathering, not shipped code. Run it from a pcode checkout:

    uv run python dev/email_probe.py you@gmail.com [--minutes 15] [--forge]

It sends one probe email to yourself (From and To you, Reply-To a fresh
`you+pcode-<token>@gmail.com` alias, our own Message-ID), then asks you to
reply to it from Gmail on the web and from the Gmail mobile app. While you do,
it watches the alias over IMAP and reports, for every message it finds:

- whether the message itself carries `\\Sent` (and its other labels),
- whether the alias survived in the parsed `To` header,
- whether `In-Reply-To`/`References` point at our Message-ID, and
- whether Gmail stored our Message-ID on the probe as sent.

`--forge` also tries to deliver a message with a forged `From: you` straight
to Gmail's MX (port 25; many networks block it), which must arrive without
`\\Sent`. A forge from another account's SMTP works as well: send to the alias
with your address in `From` and watch the report.

The password is read from the keychain entry `pcode --email-setup` writes,
else asked for. Nothing is written to disk. Paste the summary it prints into
dev/email-remote.md, with the clients you used.
"""

import argparse
import getpass
import smtplib
import sys
import time
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses

from pcode.email_remote import outbound
from pcode.email_remote.listener import alias_for, new_token
from pcode.email_remote.mailbox import GmailMailbox, SetupError, read_password

GMAIL_MX = "gmail-smtp-in.l.google.com"


def password_for(owner: str) -> str:
    try:
        return read_password(owner)
    except SetupError:
        return getpass.getpass(f"App password for {owner}: ")


def forge(owner: str, alias: str) -> None:
    message = outbound.compose(owner=owner, alias=alias, subject="pcode probe: forged", body="x")
    del message["X-Pcode-Remote"]
    del message["Auto-Submitted"]
    message.replace_header("To", alias)
    try:
        with smtplib.SMTP(GMAIL_MX, 25, timeout=30) as smtp:
            smtp.starttls()
            smtp.send_message(message, from_addr=owner, to_addrs=[alias])
        print("Forged message handed to Gmail's MX; watch for it below.")
    except (OSError, smtplib.SMTPException) as error:
        print(f"Forged delivery failed ({error}); try it from another account instead.")


def describe(mailbox: GmailMailbox, handle: str, alias: str, probe_id: str) -> str:
    meta = mailbox.meta(handle)
    raw = mailbox.headers(handle)
    message = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    to = [address.lower() for _, address in getaddresses(message.get_all("To") or [])]
    ancestry = f"{message.get('In-Reply-To', '')} {message.get('References', '')}"
    agent = message.get("User-Agent") or message.get("X-Mailer") or "-"
    return (
        f"gmail id {meta.gmail_id}: SENT={'yes' if meta.sent else 'NO'} "
        f"labels={sorted(meta.labels)} alias-in-To={'yes' if alias in to else 'NO'} "
        f"replies-to-probe={'yes' if probe_id in ancestry else 'no'} "
        f"from={message.get('From')} agent={agent} subject={message.get('Subject')!r}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("owner")
    parser.add_argument("--minutes", type=float, default=15)
    parser.add_argument("--forge", action="store_true")
    args = parser.parse_args()
    owner = args.owner.lower()
    mailbox = GmailMailbox(owner, password_for(owner))
    mailbox.verify()
    alias = alias_for(owner, new_token())
    probe = outbound.compose(
        owner=owner,
        alias=alias,
        subject="pcode probe: please reply",
        body="Reply to this from Gmail on the web (say 'web'), then from the Gmail app "
        "(say 'mobile'). The probe script is watching.",
        auto="auto-generated",
    )
    probe_id = str(probe["Message-ID"])
    mailbox.send(probe)
    print(f"Sent the probe ({probe_id}). Reply to it from Gmail web and the Gmail app.")
    if args.forge:
        forge(owner, alias)
    stored = mailbox._uid("SEARCH", "X-GM-RAW", f'"rfc822msgid:{probe_id.strip("<>")}"')
    print(f"Our Message-ID found in the account: {'yes' if stored and stored[0] else 'NO'}")
    seen: dict[str, str] = {}
    deadline = time.monotonic() + args.minutes * 60
    try:
        while time.monotonic() < deadline:
            for handle in mailbox.search(alias):
                if handle not in seen:
                    seen[handle] = describe(mailbox, handle, alias, probe_id)
                    print(seen[handle], flush=True)
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        mailbox.close()
    print("\nSummary for dev/email-remote.md (add which client sent each):")
    for line in seen.values():
        print(f"- {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
