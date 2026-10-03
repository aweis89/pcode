"""`pcode --email-listen` against a fake mailbox and fake session hosts.

Real Gmail is never contacted here; `scripts/email_probe.py` checks the
assumptions these fakes encode (the `\\Sent` label, the alias in `To`, our
Message-ID surviving) against a real account.
"""

import asyncio
import email
import json
import os
from email import policy
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

import pytest

from pcode import gateway
from pcode.email_remote import listener as listener_module
from pcode.email_remote.cli import parse_duration, profile_from_preferences
from pcode.email_remote.listener import Limits, Listener
from pcode.email_remote.mailbox import Meta, SetupError, parse_labels, read_password
from pcode.email_remote.parsing import EMPTY, MAX_TEXT_BYTES, TOO_LONG, new_text, parse, read_body
from pcode.email_remote.state import State
from pcode.host_protocol import HostEntry
from pcode.remote_profile import RemoteProfile

OWNER = "owner@gmail.com"
SENT = frozenset({"\\Inbox", "\\Sent"})
DELIVERED = frozenset({"\\Inbox"})


class FakeMailbox:
    def __init__(self) -> None:
        self.messages: dict[str, tuple[Meta, bytes]] = {}
        self.sent: list[EmailMessage] = []
        self.failures = 0
        self.fetched: list[str] = []

    def add(self, raw: bytes, labels=SENT) -> str:
        handle = str(len(self.messages) + 1)
        self.messages[handle] = (Meta(f"gm{handle}", labels, len(raw)), raw)
        return handle

    def search(self, alias):
        return list(self.messages)

    def meta(self, handle):
        return self.messages[handle][0]

    def headers(self, handle):
        return self.messages[handle][1]

    def fetch(self, handle):
        self.fetched.append(handle)
        return self.messages[handle][1]

    def send(self, message):
        if self.failures:
            self.failures -= 1
            raise OSError("smtp down")
        self.sent.append(message)

    def close(self):
        pass


class FakeSessions:
    """`pcode.gateway`'s surface, with hosts that echo and `hang` turns that wait."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.started: list[dict] = []
        self.sent: list[tuple[str, str]] = []
        self.states: dict[str, str] = {}
        self.stopped: list[str] = []
        self.gates: dict[str, asyncio.Event] = {}

    async def start_session(self, workspace, *, profile, model=None, resume=None):
        number = len(self.started) + 1
        self.started.append({"profile": profile, "model": model, "resume": resume})
        worktree = self.root / f"wt{number}"
        worktree.mkdir(exist_ok=True)
        entry = HostEntry(id=f"host{number}", pid=os.getpid(), model="m", workspace=str(worktree))
        self.states[entry.id] = "idle"
        return entry

    async def send(self, entry, text, *, base=None, on_queued=None):
        await asyncio.sleep(0)  # Attaching takes a moment.
        self.sent.append((entry.id, text))
        if on_queued is not None:
            on_queued()
        # The first input arrives as "Subject\n\ntext": its last line decides.
        if text.splitlines()[-1].startswith("hang"):
            gate = self.gates.setdefault(entry.id, asyncio.Event())
            await gate.wait()
            if self.states.get(entry.id) == "cancelled":
                self.states[entry.id] = "idle"
                return gateway.TurnResult("Started.", gateway.CANCELLED)
        changes = gateway.ChangeSummary(entry.workspace, "pcode-x", " a.txt | 1 +")
        return gateway.TurnResult(f"Echo: {text}", gateway.COMPLETED, [], changes)

    def status(self, entry):
        state = self.states.get(entry.id, "stopped")
        return gateway.SessionStatus(entry.id, f"session-{entry.id}", state, title="t", turns=1)

    def release(self, host: str) -> None:
        self.gates.setdefault(host, asyncio.Event()).set()

    async def stop(self, entry):
        self.stopped.append(entry.id)
        self.states[entry.id] = "cancelled"
        if entry.id in self.gates:
            self.gates[entry.id].set()


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def rig(tmp_path, monkeypatch):
    stopped = []

    async def stop_entry(entry, *, keep_worktree=False):
        stopped.append((entry.id, keep_worktree))

    monkeypatch.setattr(listener_module, "stop_entry", stop_entry)
    mailbox = FakeMailbox()
    sessions = FakeSessions(tmp_path)
    clock = Clock()
    logs: list[str] = []
    listener = Listener(
        owner=OWNER,
        mailbox=mailbox,
        workspace=tmp_path,
        profile=RemoteProfile(),
        ttl=3600,
        state_path=tmp_path / "state" / "state.json",
        model="m",
        log=logs.append,
        sessions=sessions,
        clock=clock,
    )
    listener.stopped_hosts = stopped
    listener.logs = logs
    return listener, mailbox, sessions, clock


def mail(
    to: str,
    text: str = "fix the bug",
    *,
    sender: str = OWNER,
    subject: str = "Task",
    reply_to: EmailMessage | None = None,
    extra: dict | None = None,
    html: str | None = None,
) -> bytes:
    message = EmailMessage()
    message["From"] = f"Owner Name <{sender}>"
    message["To"] = to
    message["Subject"] = subject
    message["Message-ID"] = make_msgid("owner", domain="mail.gmail.com")
    if reply_to is not None:
        parent = str(reply_to["Message-ID"])
        message["In-Reply-To"] = parent
        message["References"] = " ".join(
            [*str(reply_to.get("References", "")).split(), parent]
        ).strip()
    extra = dict(extra or {})
    # A second From header, which EmailMessage refuses to build.
    second_from = extra.pop("From", None)
    for key, value in extra.items():
        message[key] = value
    message.set_content(text)
    if html is not None:
        message.add_alternative(html, subtype="html")
    raw = bytes(message)
    return f"From: {second_from}\n".encode() + raw if second_from else raw


def sent_texts(mailbox: FakeMailbox) -> list[str]:
    return [message.get_body(("plain",)).get_content() for message in mailbox.sent]


async def settle(listener: Listener) -> None:
    for _ in range(3):
        tasks = [task for session in listener.sessions.values() for task in session.tasks]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)


async def poll_and_settle(listener: Listener) -> None:
    await listener.poll()
    await settle(listener)


def run(coroutine):
    # A gate never released fails the test instead of hanging the suite.
    return asyncio.run(asyncio.wait_for(coroutine, 10))


# Lifecycle


def test_each_listener_has_a_fresh_alias_and_stores_only_its_hash(rig, tmp_path):
    listener, mailbox, sessions, clock = rig
    other = Listener(
        owner=OWNER,
        mailbox=mailbox,
        workspace=tmp_path,
        profile=RemoteProfile(),
        ttl=60,
        state_path=tmp_path / "other.json",
        sessions=sessions,
    )
    assert listener.alias != other.alias
    assert listener.alias.startswith("owner+pcode-") and listener.alias.endswith("@gmail.com")
    run(listener.start())
    saved = (tmp_path / "state" / "state.json").read_text()
    assert listener._token not in saved
    assert json.loads(saved)["listener"]["token_hash"]
    assert oct((tmp_path / "state").stat().st_mode & 0o777) == "0o700"
    (launcher,) = mailbox.sent
    assert (launcher["To"], launcher["Reply-To"]) == (OWNER, listener.alias)
    assert launcher["Auto-Submitted"] == "auto-generated" and launcher["X-Pcode-Remote"]
    assert "pcode --attach" in sent_texts(mailbox)[0]
    assert f"mailto:{listener.alias}" in sent_texts(mailbox)[0]


def test_expiry_stops_intake_and_every_session(rig):
    listener, mailbox, sessions, clock = rig

    async def go():
        await listener.start()
        mailbox.add(mail(listener.alias, "hang on"))
        await listener.poll()
        while not sessions.sent:
            await asyncio.sleep(0.01)
        assert sessions.sent == [("host1", "Task\n\nhang on")]
        clock.now = listener.expires + 1
        mailbox.add(mail(listener.alias, "too late"))
        await listener.run()
        assert len(sessions.sent) == 1

    run(go())
    assert sessions.stopped == ["host1"]
    assert listener.stopped_hosts == [("host1", True)]


def test_a_new_listener_never_takes_an_old_listeners_mail(rig, tmp_path):
    listener, mailbox, sessions, clock = rig
    old_alias = listener.alias.replace("pcode-", "pcode-old")
    mailbox.add(mail(old_alias))
    run(listener.poll())
    assert listener.state.handled == {"gm1": "not-to-alias"}
    assert sessions.started == [] and mailbox.sent == []


# Acceptance


ALIAS = object()  # Stands in for the live alias in the cases below.


@pytest.mark.parametrize(
    ("to", "options", "labels", "reason"),
    [
        (ALIAS, {"sender": "attacker@evil.test"}, SENT, "sender"),
        (ALIAS, {}, DELIVERED, "not-sent"),
        (OWNER, {"extra": {"Cc": ALIAS}}, SENT, "not-to-alias"),
        (OWNER, {"text": ALIAS}, SENT, "not-to-alias"),
        (ALIAS, {"extra": {"Resent-From": OWNER}}, SENT, "sender"),
        (ALIAS, {"extra": {"Sender": "x@evil.test"}}, SENT, "sender"),
        (ALIAS, {"extra": {"From": "x@evil.test"}}, SENT, "sender"),
        (ALIAS, {"extra": {"X-Pcode-Remote": "1"}}, SENT, "own-mail"),
        (ALIAS, {"extra": {"Auto-Submitted": "auto-replied"}}, SENT, "auto-submitted"),
        (ALIAS, {"extra": {"List-Id": "<l.example>"}}, SENT, "list-mail"),
        (ALIAS, {"extra": {"Precedence": "bulk"}}, SENT, "list-mail"),
    ],
)
def test_each_failed_check_alone_rejects_silently(rig, to, options, labels, reason):
    listener, mailbox, sessions, clock = rig

    def live(value):
        return listener.alias if value is ALIAS else value

    options = {
        key: {k: live(v) for k, v in value.items()} if isinstance(value, dict) else live(value)
        for key, value in options.items()
    }
    mailbox.add(mail(live(to), **options), labels)
    run(listener.poll())
    assert listener.state.handled == {"gm1": reason}
    # No reply, no session, and the body was never fetched.
    assert mailbox.sent == [] and sessions.started == [] and mailbox.fetched == []
    (log,) = listener.logs
    assert log.startswith(f"email: ignored a message ({reason}")


def test_a_delivery_report_is_ignored(rig):
    listener, mailbox, sessions, clock = rig
    report = EmailMessage()
    report["From"] = OWNER
    report["To"] = listener.alias
    report["Message-ID"] = make_msgid()
    report.set_content("failed")
    report.replace_header("Content-Type", "multipart/report; report-type=delivery-status")
    mailbox.add(bytes(report))
    run(listener.poll())
    assert listener.state.handled == {"gm1": "report"}


def test_the_owners_own_mail_is_accepted_and_a_sibling_sent_label_does_not_help(rig):
    listener, mailbox, sessions, clock = rig
    # Our sent copy elsewhere in the thread carries \\Sent; the forged one does not.
    mailbox.add(mail(OWNER, "our earlier note"), SENT)
    mailbox.add(mail(listener.alias, "forged"), DELIVERED)
    mailbox.add(mail(listener.alias, "genuine"), SENT)
    run(poll_and_settle(listener))
    assert listener.state.handled == {"gm1": "not-to-alias", "gm2": "not-sent", "gm3": "accepted"}
    assert [text for _, text in sessions.sent] == ["Task\n\ngenuine"]


def test_a_handled_message_is_never_run_twice(rig):
    listener, mailbox, sessions, clock = rig
    raw = mail(listener.alias)
    mailbox.add(raw)

    async def go():
        await poll_and_settle(listener)
        listener.seen.clear()  # As after a reconnect: the handled set still holds it.
        mailbox.add(raw)  # The same Message-ID under another Gmail id.
        await poll_and_settle(listener)

    run(go())
    assert len(sessions.sent) == 1
    assert listener.state.handled["gm2"] == "duplicate"


def test_pcodes_own_replies_never_trigger_it(rig):
    listener, mailbox, sessions, clock = rig

    async def go():
        await listener.start()
        for message in list(mailbox.sent):
            mailbox.add(bytes(message), SENT)
        await listener.poll()

    run(go())
    assert set(listener.state.handled.values()) == {"not-to-alias"}


# Routing


def test_two_fresh_emails_are_two_sessions_even_with_one_subject(rig):
    listener, mailbox, sessions, clock = rig
    mailbox.add(mail(listener.alias, "one", subject="Same"))
    mailbox.add(mail(listener.alias, "two", subject="Same"))

    async def go():
        await listener.poll()
        await settle(listener)

    run(go())
    assert sorted(host for host, _ in sessions.sent) == ["host1", "host2"]
    assert all(started["resume"] is None for started in sessions.started)


def test_a_reply_continues_its_session_whatever_its_subject(rig):
    listener, mailbox, sessions, clock = rig

    async def go():
        mailbox.add(mail(listener.alias, "first"))
        await listener.poll()
        await settle(listener)
        result = mailbox.sent[-1]
        mailbox.add(mail(listener.alias, "second", subject="Changed", reply_to=result))
        await listener.poll()
        await settle(listener)

    run(go())
    assert sessions.sent == [("host1", "Task\n\nfirst"), ("host1", "second")]
    assert len(sessions.started) == 1
    # Replies are threaded under the message they answer.
    first_input = email.message_from_bytes(mailbox.messages["1"][1], policy=policy.default)
    assert mailbox.sent[0]["In-Reply-To"] == first_input["Message-ID"]
    assert mailbox.sent[0]["Subject"] == "Re: Task"


def test_rapid_replies_to_the_launcher_bind_one_session(rig):
    listener, mailbox, sessions, clock = rig

    async def go():
        await listener.start()
        launcher = mailbox.sent[0]
        mailbox.add(mail(listener.alias, "hang a", reply_to=launcher))
        mailbox.add(mail(listener.alias, "b", reply_to=launcher))
        await listener.poll()
        assert len(listener.sessions) == 1
        await asyncio.sleep(0.05)
        sessions.release("host1")
        await settle(listener)

    run(go())
    assert [host for host, _ in sessions.sent] == ["host1", "host1"]


def test_ancestry_spanning_two_sessions_is_refused(rig):
    listener, mailbox, sessions, clock = rig

    async def go():
        mailbox.add(mail(listener.alias, "one"))
        mailbox.add(mail(listener.alias, "two"))
        await listener.poll()
        await settle(listener)
        first, second = mailbox.sent[-2:]
        crossed = mail(listener.alias, "both", reply_to=first)
        message = email.message_from_bytes(crossed, policy=policy.default)
        message.replace_header("References", f"{first['Message-ID']} {second['Message-ID']}")
        mailbox.add(bytes(message))
        await listener.poll()
        await settle(listener)

    run(go())
    assert len(sessions.sent) == 2
    assert "more than one pcode session" in sent_texts(mailbox)[-1]


def test_an_unknown_thread_asks_for_a_fresh_email(rig):
    listener, mailbox, sessions, clock = rig
    stranger = EmailMessage()
    stranger["Message-ID"] = "<old@example>"
    mailbox.add(mail(listener.alias, "continue", reply_to=stranger))
    run(listener.poll())
    assert sessions.started == []
    assert "does not recognise the thread" in sent_texts(mailbox)[-1]


# Controls


def test_status_and_stop_never_reach_a_model(rig, monkeypatch):
    listener, mailbox, sessions, clock = rig
    # The session's acknowledgement is the email the owner replies to.
    monkeypatch.setattr(listener_module, "COALESCE_SECONDS", 0.01)

    async def go():
        mailbox.add(mail(listener.alias, "/status"))
        mailbox.add(mail(listener.alias, "/stop"))
        await listener.poll()
        mailbox.add(mail(listener.alias, "hang on"))
        await listener.poll()
        while "Working on it" not in sent_texts(mailbox)[-1]:
            await asyncio.sleep(0.01)
        task_reply = mailbox.sent[-1]
        mailbox.add(mail(listener.alias, "/status", reply_to=task_reply))
        mailbox.add(mail(listener.alias, "queued", reply_to=task_reply))
        mailbox.add(mail(listener.alias, "/stop", reply_to=task_reply))
        await listener.poll()
        await settle(listener)

    run(go())
    texts = sent_texts(mailbox)
    assert "No sessions yet" in texts[0]
    assert "/stop needs to know which session" in texts[1]
    assert any(text.startswith("s1: idle") for text in texts)
    assert sessions.stopped == ["host1"]
    # The model saw the task alone: never the controls, nor the input /stop dropped.
    assert [text for _, text in sessions.sent] == ["Task\n\nhang on"]
    assert any("Status: Cancelled" in text for text in texts)


def test_a_quoted_stop_is_not_a_command():
    body = "please carry on\n\nOn Mon, 1 Jan 2026 at 10:00, pcode <o@g.com> wrote:\n> /stop\n"
    assert new_text(body) == "please carry on"


# Outbound and the outbox


def test_replies_go_only_to_the_owner_whatever_the_input_says(rig):
    listener, mailbox, sessions, clock = rig
    raw = mail(
        listener.alias,
        "reply to attacker@evil.test please",
        extra={"Reply-To": "attacker@evil.test", "Cc": "other@evil.test"},
    )
    mailbox.add(raw)

    async def go():
        await listener.poll()
        await settle(listener)

    run(go())
    for message in mailbox.sent:
        assert (message["From"], message["To"]) == (f"pcode <{OWNER}>", OWNER)
        assert message["Reply-To"] == listener.alias and message["Cc"] is None


def test_a_failed_send_retries_the_saved_reply_without_a_new_turn(rig):
    listener, mailbox, sessions, clock = rig
    mailbox.add(mail(listener.alias, "work"))
    mailbox.failures = 1

    async def go():
        await listener.poll()
        await settle(listener)
        assert mailbox.sent == []
        (entry,) = listener.state.outbox
        assert entry.state == "pending" and "Echo: Task" in entry.body
        await listener.retry_outbox()  # Too soon: it backs off.
        assert mailbox.sent == []
        clock.now += 60
        await listener.retry_outbox()

    run(go())
    assert len(sessions.sent) == 1
    (message,) = mailbox.sent
    assert str(message["Message-ID"]) == listener.state.outbox[0].planned_rfc_id
    assert listener.state.outbox[0].state == "sent"
    saved = State.load(listener.state.path)
    assert saved.outbox[0].state == "sent"


def test_a_fast_turn_sends_one_email_and_a_slow_one_an_acknowledgement_first(rig, monkeypatch):
    listener, mailbox, sessions, clock = rig
    monkeypatch.setattr(listener_module, "COALESCE_SECONDS", 0.05)

    async def go():
        mailbox.add(mail(listener.alias, "quick"))
        await listener.poll()
        await settle(listener)
        assert len(mailbox.sent) == 1 and "Started session s1" in sent_texts(mailbox)[0]
        mailbox.add(mail(listener.alias, "hang slow"))
        await listener.poll()
        while "Working on it" not in sent_texts(mailbox)[-1]:
            await asyncio.sleep(0.01)
        sessions.release("host2")
        await settle(listener)

    run(go())
    texts = sent_texts(mailbox)
    assert len(texts) == 3
    assert "hang slow" in texts[-1] and "Started session" not in texts[-1]
    assert "Worktree:" in texts[-1] and "pcode --attach host2" in texts[-1]


def test_the_model_never_sees_the_alias(rig):
    listener, mailbox, sessions, clock = rig
    mailbox.add(mail(listener.alias, f"mail {listener.alias} for me"))

    async def go():
        await listener.poll()
        await settle(listener)

    run(go())
    ((_, text),) = sessions.sent
    assert listener.alias not in text and "[pcode address]" in text


def test_a_session_whose_host_idled_out_is_resumed(rig):
    listener, mailbox, sessions, clock = rig

    async def go():
        mailbox.add(mail(listener.alias, "first"))
        await listener.poll()
        await settle(listener)
        sessions.states["host1"] = "stopped"
        mailbox.add(mail(listener.alias, "again", reply_to=mailbox.sent[-1]))
        await listener.poll()
        await settle(listener)

    run(go())
    assert [s["resume"] for s in sessions.started] == [None, "session-host1"]
    assert sessions.sent[-1] == ("host2", "again")


# Limits


def test_over_a_limit_is_refused_with_a_reply(rig):
    listener, mailbox, sessions, clock = rig
    listener.limits = Limits(concurrent_sessions=1, max_sessions=20)

    async def go():
        mailbox.add(mail(listener.alias, "hang one"))
        mailbox.add(mail(listener.alias, "two"))
        await listener.poll()
        await asyncio.sleep(0.05)
        assert "1 sessions are already working" in sent_texts(mailbox)[-1]
        sessions.release("host1")
        await settle(listener)

    run(go())
    assert len(listener.sessions) == 1 and len(sessions.started) == 1


# Parsing fixtures


GMAIL_PLAIN = """Please also add tests.

On Fri, 2 Oct 2026 at 09:12, pcode <owner@gmail.com>
wrote:

> Echo: fix the bug
>
> --
> Reply to this email to continue.
"""


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (GMAIL_PLAIN, "Please also add tests."),
        ("ok\n\n-- \nOwner Name\nphone\n", "ok"),
        ("keep\n--\nthis line\n", "keep\n--\nthis line"),
        ("look at this:\n> quoted by me\nthanks", "look at this:\n> quoted by me\nthanks"),
        ("```\nif a > b:\n    pass\n```\n", "```\nif a > b:\n    pass\n```"),
        ("do it\n\nSent from my iPhone\n", "do it"),
        ("On Fri, someone wrote:\n> only quote\n", ""),
    ],
)
def test_new_text_fixtures(body, expected):
    assert new_text(body) == expected


def test_html_replies_drop_the_gmail_quote_and_fetch_nothing():
    raw = mail(
        "a@b.c",
        "plain unused",
        html='<div dir="ltr">Run <b>the</b> tests<br>please</div><br>'
        '<div class="gmail_quote"><blockquote>old &gt; stuff</blockquote></div>'
        '<img src="https://tracker.example/x.png"><script>alert(1)</script>',
    )
    message = email.message_from_bytes(raw, policy=policy.default)
    # Only the HTML alternative: drop the plain part.
    html_only = EmailMessage()
    for key in ("From", "To", "Message-ID"):
        html_only[key] = message[key]
    html_only.set_content(message.get_body(("html",)).get_content(), subtype="html")
    incoming = read_body(bytes(html_only), parse(bytes(html_only)))
    assert incoming.text == "Run the tests\nplease"


def test_empty_oversized_and_attachment_only_bodies_ask_for_a_resend(rig):
    listener, mailbox, sessions, clock = rig
    attachment = EmailMessage()
    attachment["From"] = OWNER
    attachment["To"] = listener.alias
    attachment["Message-ID"] = make_msgid()
    attachment["Subject"] = "file"
    attachment.add_attachment(b"\x00\x01", maintype="application", subtype="zip", filename="x.zip")
    raw_attachment = bytes(attachment)
    assert read_body(raw_attachment, parse(raw_attachment)).problem == EMPTY
    big = mail(listener.alias, "x" * (MAX_TEXT_BYTES + 1))
    assert read_body(big, parse(big)).problem == TOO_LONG

    async def go():
        mailbox.add(mail(listener.alias, "On Fri, a wrote:\n> quote only\n"))
        mailbox.add(raw_attachment)
        mailbox.add(big)
        await listener.poll()

    run(go())
    assert sessions.started == []
    texts = sent_texts(mailbox)
    assert len(texts) == 3 and all(text.startswith("Nothing was run") for text in texts)


def test_attachments_are_ignored_and_mentioned(rig):
    listener, mailbox, sessions, clock = rig
    message = email.message_from_bytes(mail(listener.alias, "see file"), policy=policy.default)
    message.add_attachment(b"data", maintype="application", subtype="pdf", filename="spec.pdf")
    mailbox.add(bytes(message))

    async def go():
        await listener.poll()
        await settle(listener)

    run(go())
    assert "Attachments were ignored: spec.pdf" in sent_texts(mailbox)[-1]


# Mailbox, credentials and setup


def test_gmail_labels_are_read_from_atoms_and_quoted_strings():
    line = '12 (X-GM-MSGID 99 X-GM-LABELS (\\Inbox "\\\\Sent" "my (label)") UID 4 RFC822.SIZE 10)'
    labels = parse_labels(line)
    assert labels == {"\\Inbox", "\\Sent", "my (label)"}
    assert Meta("99", labels, 10).sent
    assert not Meta("99", parse_labels("1 (X-GM-LABELS (\\Inbox) UID 1)"), 1).sent


def test_without_a_keychain_there_is_no_plaintext_fallback(monkeypatch):
    from pcode.email_remote import mailbox

    monkeypatch.setattr(mailbox.sys, "platform", "linux")
    with pytest.raises(SetupError, match="no plaintext fallback"):
        read_password(OWNER)


def test_durations_and_the_profile_come_from_local_settings(tmp_path):
    assert parse_duration("8h") == 8 * 3600 and parse_duration("90") == 5400
    with pytest.raises(ValueError):
        parse_duration("forever")
    profile = profile_from_preferences(
        {"email_mcp": "on", "email_turn_requests": "5", "email_turn_minutes": "0"}, base="abc"
    )
    assert (profile.mcp, profile.max_requests, profile.turn_minutes) == (True, 5, 0)
    assert profile.base == "abc" and profile.deny_read[0].endswith("email-remote")


def test_email_settings_cannot_come_from_a_repository():
    from pcode.preferences import USER_ONLY

    assert {"email_owner", "email_mcp", "email_turn_requests"} <= USER_ONLY


def test_the_alias_is_redacted_even_when_split_across_quoted_lines(rig):
    listener, mailbox, sessions, clock = rig
    token = listener._token
    broken = f"see {token[:10]}=\n> {token[10:].upper()} please"
    incoming = parse(mail(listener.alias))
    incoming.text = broken
    assert token[:10] not in listener.prompt(listener.sessions.get("x"), incoming, False)
    assert "[pcode address]" in listener.prompt(None, incoming, False)


def test_one_unreadable_message_does_not_hold_up_the_rest(rig, monkeypatch):
    listener, mailbox, sessions, clock = rig
    mailbox.add(mail(listener.alias, "broken"))
    mailbox.add(mail(listener.alias, "fine"))
    real = mailbox.meta

    def meta(handle):
        if handle == "1":
            raise OSError("unexpected FETCH shape")
        return real(handle)

    monkeypatch.setattr(mailbox, "meta", meta)
    run(poll_and_settle(listener))
    assert [text for _, text in sessions.sent] == ["Task\n\nfine"]
    for _ in range(listener_module.READ_ATTEMPTS):
        run(listener.poll())
    assert "1" in listener.seen  # Given up on, not retried forever.


def test_a_body_that_cannot_be_fetched_gets_a_resend_request(rig, monkeypatch):
    listener, mailbox, sessions, clock = rig
    mailbox.add(mail(listener.alias, "work"))

    def fetch(handle):
        raise OSError("connection reset")

    monkeypatch.setattr(mailbox, "fetch", fetch)
    run(poll_and_settle(listener))
    assert sessions.sent == []
    assert sent_texts(mailbox)[-1].startswith("Nothing was run")


def test_stop_while_the_host_is_starting_means_the_input_never_runs(rig):
    listener, mailbox, sessions, clock = rig
    started = asyncio.Event()
    proceed = asyncio.Event()
    real_start = sessions.start_session

    async def slow_start(*args, **kwargs):
        started.set()
        await proceed.wait()
        return await real_start(*args, **kwargs)

    sessions.start_session = slow_start

    async def go():
        mailbox.add(mail(listener.alias, "work"))
        await listener.poll()
        await started.wait()
        # The owner replies /stop to the launcher-less session's first email.
        session = listener.sessions["s1"]
        await listener.stop_session(session, listener.replier(parse(mailbox.messages["1"][1])))
        proceed.set()
        await settle(listener)

    run(go())
    assert sessions.sent == []
    assert any(text.startswith("Not run: the session was stopped") for text in sent_texts(mailbox))


def test_a_reply_being_sent_is_not_sent_again_by_a_retry(rig, monkeypatch):
    listener, mailbox, sessions, clock = rig
    release = asyncio.Event()
    calls = []

    async def go():
        loop = asyncio.get_running_loop()

        def slow_send(message):
            calls.append(message)
            asyncio.run_coroutine_threadsafe(release.wait(), loop).result()

        monkeypatch.setattr(mailbox, "send", slow_send)
        sending = asyncio.create_task(listener.send("hi", subject="s", route="control"))
        while not calls:
            await asyncio.sleep(0.01)
        await listener.retry_outbox(force=True)
        release.set()
        await sending

    run(go())
    assert len(calls) == 1 and listener.state.outbox[0].state == "sent"


def test_shutdown_says_so_by_email(rig):
    listener, mailbox, sessions, clock = rig

    async def go():
        mailbox.add(mail(listener.alias, "work"))
        await poll_and_settle(listener)
        await listener.shutdown()

    run(go())
    assert "pcode stopped listening" in sent_texts(mailbox)[-1]
    assert "s1 (worktree" in sent_texts(mailbox)[-1]
