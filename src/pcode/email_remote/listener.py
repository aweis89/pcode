"""The listener: which mail it accepts, where each message goes, and the sessions it runs.

Acceptance (`reject_reason`) is decided from the mailbox's labels and the
parsed headers before the body is read by anything but the parser. Routing
follows RFC ancestry (`References`, `In-Reply-To`) through the ids pcode
recorded, never Gmail's thread or the subject. A task goes to a session (a
host started through `pcode.gateway` under the fixed remote profile); `/status`
and `/stop` never reach a model. Every reply is recorded in the outbox before
it is sent, so a failed send is retried with the same content and never by
running the agent again.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import random
import re
import secrets
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from pcode import gateway
from pcode.email_remote import outbound
from pcode.email_remote.mailbox import Mailbox, Meta
from pcode.email_remote.parsing import (
    EMPTY,
    MALFORMED,
    MAX_MESSAGE_BYTES,
    TOO_LONG,
    Incoming,
    parse,
    read_body,
)
from pcode.email_remote.state import CONTROL, LAUNCHER, OutboxEntry, State
from pcode.error_report import error_message
from pcode.host_protocol import HostEntry
from pcode.remote import stop_entry
from pcode.remote_profile import RemoteProfile

POLL_SECONDS = 5.0
MAX_BACKOFF = 120.0
# A turn that ends this soon after its session started gets one email, not two.
COALESCE_SECONDS = 20.0
SEND_ATTEMPTS = 6
# Seconds before the first resend of a failed reply; doubled each time after.
SEND_BACKOFF = 15.0
READ_ATTEMPTS = 3
CONTROLS = ("/status", "/stop")
REDACTED = "[pcode address]"

OUTCOMES = {
    gateway.COMPLETED: "Completed",
    gateway.FAILED: "Failed",
    gateway.CANCELLED: "Cancelled",
    gateway.LIMIT: "Stopped at a limit",
}
PROBLEMS = {
    EMPTY: "it had no new text above the quoted thread",
    MALFORMED: "pcode could not read its text",
    TOO_LONG: "its new text is over 64 KiB",
    "too-large": "the whole message is over 2 MiB",
}


@dataclass(frozen=True)
class Limits:
    concurrent_sessions: int = 2
    max_sessions: int = 20
    session_inputs: int = 20
    max_inputs: int = 100

    @classmethod
    def from_preferences(cls, preferences: dict) -> Limits:
        return cls(
            concurrent_sessions=int(preferences.get("email_concurrent_sessions") or 2),
            max_sessions=int(preferences.get("email_max_sessions") or 20),
            session_inputs=int(preferences.get("email_session_inputs") or 20),
            max_inputs=int(preferences.get("email_max_inputs") or 100),
        )


@dataclass
class Session:
    """One email-started session: its host while one runs, and what reaches it again."""

    key: str
    subject: str
    entry: HostEntry | None = None
    session_id: str = ""
    # The worktree's commit when it started: change summaries are against it.
    base: str | None = None
    # Inputs accepted and not finished; they run one after another.
    pending: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: set[asyncio.Task] = field(default_factory=set)
    # The input whose turn is running (it holds `lock`).
    current: asyncio.Task | None = None
    # Counts /stop; an input accepted before the latest one does not run.
    stops: int = 0
    # Set once the running input's message is in the host's queue, where a
    # cancel reaches it; None while no message is on its way.
    queued: asyncio.Event | None = None

    def finished(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        self.pending -= 1
        if self.current is task:
            self.current = None

    def record(self) -> dict:
        entry = self.entry
        return {
            "subject": self.subject,
            "host": entry.id if entry else "",
            "session_id": self.session_id,
            "worktree": entry.workspace if entry else "",
            "base": self.base,
        }


def new_token() -> str:
    """16 random bytes as lowercase Base32: the alias's secret part."""
    return base64.b32encode(secrets.token_bytes(16)).decode().rstrip("=").lower()


def alias_for(owner: str, token: str) -> str:
    local, _, domain = owner.partition("@")
    return f"{local}+pcode-{token}@{domain}"


def head_commit(workspace: Path) -> str | None:
    return gateway.inert_git(workspace, "rev-parse", "HEAD") or None


class Listener:
    """One `--email-listen` invocation: a fresh alias, its sessions, until it expires."""

    def __init__(
        self,
        *,
        owner: str,
        mailbox: Mailbox,
        workspace: Path,
        profile: RemoteProfile,
        ttl: float,
        state_path: Path,
        limits: Limits = Limits(),
        model: str | None = None,
        log: Callable[[str], None] | None = None,
        sessions=gateway,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.owner = owner.lower()
        self.mailbox = mailbox
        self.workspace = workspace
        self.profile = profile
        self.limits = limits
        self.model = model
        self.log = log or (lambda text: print(text, file=sys.stderr, flush=True))
        self.api = sessions
        self.clock = clock
        # The raw token exists here and in delivered mail only; the state has its hash.
        self._token = new_token()
        self.alias = alias_for(self.owner, self._token)
        self.started = clock()
        self.expires = self.started + ttl
        self.stopping = False
        self.state = State(
            path=state_path,
            listener={
                "token_hash": hashlib.sha256(self._token.encode()).hexdigest(),
                "owner": self.owner,
                "workspace": str(workspace),
                "profile": profile.to_json(),
                "started": self.started,
                "expires": self.expires,
            },
        )
        self.sessions: dict[str, Session] = {}
        # Message-IDs pcode sent, and IMAP handles already looked at.
        self.sent_ids: set[str] = set()
        self.seen: set[str] = set()
        # Handles whose metadata could not be read, and how often.
        self.failures: dict[str, int] = {}
        self.handled_ids: set[str] = set()
        # SMTP runs in a thread: cancelling its caller cannot stop the send.
        # Keep the operation alive until its outcome has been saved.
        self.deliveries: dict[str, asyncio.Task[None]] = {}

    # Lifetime

    def active(self) -> bool:
        return not self.stopping and self.clock() < self.expires

    async def start(self) -> None:
        self.state.save()
        lines = [
            f"pcode is listening for email in {self.workspace} until "
            f"{time.strftime('%a %H:%M %Z', time.localtime(self.expires))}.",
            "",
            "Reply to this email with a task to start a session; reply to a session's "
            "emails to continue it. To start another session alongside, write a new "
            "email to the address this one replies to.",
            "",
            "Send just `/status` or `/stop` as the whole text to see or stop a session's "
            "work (in a fresh email: every session).",
            "",
            "Sessions run unattended under this profile, which email cannot change:",
            *(f"- {line}" for line in self.profile.describe()),
            "",
            "Each session is a background host: `pcode --hosts` lists them, and "
            "`pcode --attach <id>` takes one over at a terminal.",
        ]
        await self.send(
            "\n".join(lines),
            subject=f"pcode remote: {self.workspace.name}",
            route=LAUNCHER,
            auto="auto-generated",
        )

    async def run(self, stop: asyncio.Event | None = None) -> None:
        """Poll until the listener expires or `stop` is set; then end every session."""
        stop = stop or asyncio.Event()
        delay = POLL_SECONDS
        try:
            while self.active() and not stop.is_set():
                try:
                    await self.poll()
                    delay = POLL_SECONDS
                except Exception as error:  # noqa: BLE001 - the mailbox may come back.
                    delay = min(delay * 2, MAX_BACKOFF)
                    self.log(
                        f"email: poll failed ({error_message(error)}); retrying in {delay:.0f}s"
                    )
                await self.retry_outbox()
                remaining = self.expires - self.clock()
                pause = min(delay * random.uniform(0.8, 1.2), max(remaining, 0))
                try:
                    await asyncio.wait_for(stop.wait(), timeout=pause)
                except TimeoutError:
                    pass
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Stop intake, cancel waiting inputs, stop every session; keep their worktrees."""
        self.stopping = True
        tasks = [task for session in self.sessions.values() for task in session.tasks]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        stopped = []
        for session in self.sessions.values():
            entry = session.entry
            if entry is None or self.api.status(entry).state == "stopped":
                continue
            try:
                await self.api.stop(entry)
                await stop_entry(entry, keep_worktree=True)
                stopped.append(f"{session.key} (worktree `{entry.workspace}`)")
            except (OSError, ConnectionError) as error:
                self.log(f"email: could not stop host {entry.id}: {error_message(error)}")
        # Cancelled session tasks may have left SMTP running. Settle those
        # operations before retrying anything or sending the final notification.
        if self.deliveries:
            await asyncio.gather(*(asyncio.shield(task) for task in self.deliveries.values()))
        lines = ["pcode stopped listening; replies to its emails are no longer read."]
        if stopped:
            lines += [
                "",
                "Stopped sessions, their transcripts and worktrees kept:",
                "",
                *(f"- {line}" for line in stopped),
            ]
        await self.send("\n".join(lines), subject="pcode remote: stopped", route=CONTROL)
        # There is no retry loop after shutdown. Spend the remaining finite
        # attempt budget now, including the final notification, without backoff.
        # Each SMTP operation retains the mailbox's network timeout; abandoning
        # its thread on an asyncio timeout would lose the actual send outcome.
        for _ in range(SEND_ATTEMPTS):
            if not any(entry.state == "pending" for entry in self.state.outbox):
                break
            await self.retry_outbox(force=True)
        self.state.save()
        self.log("email: listener stopped; sessions' transcripts and worktrees are kept.")

    # Intake

    async def poll(self) -> None:
        handles = await asyncio.to_thread(self.mailbox.search, self.alias)
        for handle in handles:
            if handle in self.seen or not self.active():
                continue
            # One message that cannot be read must not hold up those after it.
            try:
                meta = await asyncio.to_thread(self.mailbox.meta, handle)
            except Exception as error:  # noqa: BLE001 - counted, then given up on.
                failures = self.failures[handle] = self.failures.get(handle, 0) + 1
                self.log(f"email: could not read message {handle} ({error_message(error)})")
                if failures >= READ_ATTEMPTS:
                    self.seen.add(handle)
                continue
            self.seen.add(handle)
            if meta.gmail_id in self.state.handled:
                continue
            try:
                await self.take(handle, meta)
            except Exception as error:  # noqa: BLE001 - recorded; the loop carries on.
                self.state.handled.setdefault(meta.gmail_id, "error")
                self.state.save()
                self.log(f"email: could not read message {handle} ({error_message(error)})")

    async def take(self, handle: str, meta: Meta) -> None:
        headers = await asyncio.to_thread(self.mailbox.headers, handle)
        incoming = parse(headers)
        reason = self.reject_reason(incoming, meta)
        # Recorded before anything is dispatched, so nothing runs twice.
        self.state.handled[meta.gmail_id] = reason or "accepted"
        self.state.save()
        if reason:
            hint = f"; From must be exactly {self.owner}" if reason == "sender" else ""
            self.log(f"email: ignored a message ({reason}{hint})")
            return
        self.handled_ids.add(incoming.message_id)
        if meta.size > MAX_MESSAGE_BYTES:
            incoming.problem = "too-large"
        else:
            try:
                raw = await asyncio.to_thread(self.mailbox.fetch, handle)
                if len(raw) > MAX_MESSAGE_BYTES:
                    incoming.problem = "too-large"
                else:
                    read_body(raw, incoming)
            except Exception as error:  # noqa: BLE001 - the owner is asked to resend.
                self.log(f"email: could not fetch a message ({error_message(error)})")
                incoming.problem = MALFORMED
        await self.dispatch(incoming)

    def reject_reason(self, incoming: Incoming, meta: Meta) -> str:
        """Why this message is not the owner's own to this listener; "" to accept it."""
        if not self.active():
            return "listener-inactive"
        if self.alias not in incoming.to:
            return "not-to-alias"
        if incoming.senders != [self.owner] or incoming.resent:
            return "sender"
        if incoming.sender_header and incoming.sender_header != [self.owner]:
            return "sender"
        if not meta.sent:
            return "not-sent"
        if incoming.marker or incoming.message_id in self.sent_ids:
            return "own-mail"
        if incoming.auto_submitted not in ("", "no"):
            return "auto-submitted"
        if incoming.report:
            return "report"
        if incoming.list_mail:
            return "list-mail"
        if not incoming.message_id:
            return "no-message-id"
        if incoming.message_id in self.handled_ids:
            return "duplicate"
        return ""

    # Routing

    def resolve(self, incoming: Incoming) -> tuple[str, Session | None, str]:
        """(kind, session, root): kind is "fresh", "session", "root", "unknown" or "conflict"."""
        if not incoming.ancestry:
            return "fresh", None, ""
        targets = [self.state.routes[i] for i in incoming.ancestry if i in self.state.routes]
        keys = {target for target in targets if target in self.sessions}
        if len(keys) > 1:
            return "conflict", None, ""
        if keys:
            return "session", self.sessions[keys.pop()], ""
        roots = [i for i in incoming.ancestry if self.state.routes.get(i) in (LAUNCHER, CONTROL)]
        if roots:
            return "root", None, roots[-1]
        return "unknown", None, ""

    async def dispatch(self, incoming: Incoming) -> None:
        kind, session, root = self.resolve(incoming)
        reply = self.replier(incoming)
        if kind == "conflict":
            await reply(
                "This email replies to messages from more than one pcode session, so "
                "pcode cannot tell which to continue. Reply to one session's latest email, "
                "or write a new email for a new session.",
                CONTROL,
            )
            return
        if kind == "unknown":
            await reply(
                "pcode does not recognise the thread this email replies to (it may be from "
                "an earlier listener). Write a new email to start a session.",
                CONTROL,
            )
            return
        if incoming.problem:
            what = PROBLEMS.get(incoming.problem, "pcode could not read it")
            await reply(
                f"Nothing was run: {what}. Please send the task again as plain new text.",
                session.key if session else CONTROL,
            )
            return
        command = incoming.text.strip()
        if command == "/status":
            await reply(self.status_text(session), session.key if session else CONTROL)
            return
        if command == "/stop":
            await self.stop_session(session, reply)
            return
        if refusal := self.refusal(session):
            route = session.key if session else CONTROL
            await reply(f"Not run: {refusal}. Send it again later.", route)
            return
        if session is None:
            session = Session(key=f"s{len(self.sessions) + 1}", subject=incoming.subject)
            self.sessions[session.key] = session
            if kind == "root":
                # Replies to the same launcher (or status email) join this session.
                self.state.routes[root] = session.key
        self.state.routes[incoming.message_id] = session.key
        self.state.save()
        session.pending += 1
        task = asyncio.create_task(self.run_input(session, incoming, reply, session.stops))
        session.tasks.add(task)
        # Not a `finally` in run_input: a task cancelled before it starts never runs one.
        task.add_done_callback(session.finished)

    # Sessions

    def refusal(self, session: Session | None) -> str:
        """Why a new input for `session` (None: a new session) is over a limit; "" if not."""
        limits = self.limits
        pending = sum(each.pending for each in self.sessions.values())
        busy = sum(1 for each in self.sessions.values() if each.pending)
        if session is None and len(self.sessions) >= limits.max_sessions:
            return (
                f"this listener has already started {limits.max_sessions} sessions; "
                "restart pcode --email-listen for more"
            )
        if session is not None and session.pending >= limits.session_inputs:
            return f"this session already has {session.pending} emails waiting"
        if pending >= limits.max_inputs:
            return f"{pending} emails are already waiting across all sessions"
        if (session is None or not session.pending) and busy >= limits.concurrent_sessions:
            return f"{limits.concurrent_sessions} sessions are already working"
        return ""

    async def ensure_host(self, session: Session) -> bool:
        """A running host for `session`; True when it was started fresh rather than resumed."""
        entry = session.entry
        if entry is not None and self.api.status(entry).state != "stopped":
            return False
        resume = session.session_id or None
        session.entry = await self.api.start_session(
            self.workspace, profile=self.profile, model=self.model, resume=resume
        )
        if session.base is None:
            session.base = await asyncio.to_thread(head_commit, Path(session.entry.workspace))
        self.state.sessions[session.key] = session.record()
        self.state.save()
        self.log(f"email: session {session.key} on host {session.entry.id}")
        return resume is None

    def prompt(self, session: Session, incoming: Incoming, first: bool) -> str:
        text = incoming.text
        subject = incoming.subject
        if first and subject and not subject.lower().startswith("re:") and subject not in text:
            text = f"{subject}\n\n{text}"
        # The model never sees the address that controls it, even with the
        # token broken across quoted, wrapped or encoded lines.
        spread = r"[\s>=]*".join(re.escape(char) for char in self._token)
        return re.sub(spread, REDACTED, text, flags=re.IGNORECASE)

    async def run_input(self, session: Session, incoming: Incoming, reply, stops: int) -> None:
        try:
            async with session.lock:
                session.current = asyncio.current_task()
                fresh = await self.ensure_host(session)
                entry = session.entry
                # No await from this check to the send: a /stop either sees
                # `queued` and waits for it, or this sees its count.
                if session.stops != stops:
                    await reply("Not run: the session was stopped first.", session.key)
                    return
                queued = session.queued = asyncio.Event()
                sending = asyncio.ensure_future(
                    self.api.send(
                        entry,
                        self.prompt(session, incoming, fresh),
                        base=session.base,
                        on_queued=queued.set,
                    )
                )
                acknowledged = False
                if fresh:
                    done, _ = await asyncio.wait({sending}, timeout=COALESCE_SECONDS)
                    if not done:
                        acknowledged = True
                        await reply(self.started_text(session), session.key)
                try:
                    result = await sending
                finally:
                    sending.cancel()
                    session.queued = None
                status = self.api.status(entry)
                session.session_id = status.session_id or session.session_id
                self.state.sessions[session.key] = session.record()
                self.state.save()
                body, trailer = self.result_text(
                    session, result, incoming, intro=fresh and not acknowledged
                )
                await reply(body, session.key, trailer=trailer)
        except Exception as error:  # noqa: BLE001 - told to the owner, not raised.
            self.log(f"email: session {session.key} failed: {error_message(error)}")
            await reply(f"pcode could not run this: {error_message(error)}", session.key)

    async def stop_session(self, session: Session | None, reply) -> None:
        if session is None:
            await reply(
                "/stop needs to know which session: reply with /stop to an email from "
                "that session.",
                CONTROL,
            )
            return
        # Inputs still waiting their turn here are dropped, as the host's queue
        # is; the running one ends when its turn is cancelled, and says so.
        session.stops += 1
        for task in session.tasks:
            if task is not session.current:
                task.cancel()
        if session.queued is not None:
            # Its message may still be on its way: cancel once the host holds it.
            try:
                await asyncio.wait_for(session.queued.wait(), timeout=30)
            except TimeoutError:
                pass
        if session.entry is not None and self.api.status(session.entry).state != "stopped":
            await self.api.stop(session.entry)
        await reply(f"Stopped session {session.key}'s work; the session stays open.", session.key)

    # What the replies say

    def started_text(self, session: Session) -> str:
        entry = session.entry
        return (
            f"Started session {session.key} (host `{entry.id}`) in `{entry.workspace}`. "
            f"Working on it; the result follows by email.\n\n"
            f"Take it over at a terminal: `pcode --attach {entry.id}`"
        )

    def result_text(
        self, session: Session, result, incoming: Incoming, *, intro: bool
    ) -> tuple[str, str]:
        """The model's reply (Markdown) and the session detail under it."""
        entry = session.entry
        body = result.reply or "(The turn ended without a reply.)"
        if intro:
            body = f"Started session {session.key}.\n\n{body}"
        lines = [f"Status: {OUTCOMES.get(result.outcome, result.outcome)}"]
        lines += [f"Note: {note}" for note in result.notes]
        if incoming.attachments:
            lines.append(f"Attachments were ignored: {', '.join(incoming.attachments)}")
        session_id = session.session_id or "(unsaved)"
        lines.append(f"Session {session.key}: {session_id}, host {entry.id}")
        if result.changes is not None:
            lines += ["", result.changes.describe()]
        lines += ["", f"Take it over at a terminal: pcode --attach {entry.id}"]
        return body, "\n".join(lines)

    def status_text(self, session: Session | None) -> str:
        sessions = [session] if session is not None else list(self.sessions.values())
        if not sessions:
            return "No sessions yet. Reply with a task to start one."
        lines = []
        for each in sessions:
            if each.entry is None:
                lines.append(f"- **{each.key}**: starting ({each.pending} waiting)")
                continue
            status = self.api.status(each.entry)
            line = f"- **{each.key}**: {status.state}, {status.turns} turns"
            if status.outcome:
                line += f", last {status.outcome}"
            if each.pending:
                line += f", {each.pending} waiting"
            lines.append(f"{line} — {status.title or each.subject}")
            lines.append(f"  - host `{each.entry.id}`, worktree `{status.workspace}`")
        remaining = max(0, self.expires - self.clock()) / 60
        lines.append(f"\nListening for {remaining:.0f} more minutes.")
        return "\n".join(lines)

    # Sending

    def replier(self, incoming: Incoming):
        async def reply(body: str, route: str, *, trailer: str = "") -> None:
            await self.send(
                body,
                trailer=trailer,
                subject=outbound.reply_subject(incoming.subject),
                route=route,
                in_reply_to=incoming.message_id,
                references=incoming.ancestry,
            )

        return reply

    async def send(
        self,
        body: str,
        *,
        subject: str,
        route: str,
        trailer: str = "",
        in_reply_to: str = "",
        references: list[str] | None = None,
        auto: str = "auto-replied",
    ) -> None:
        message_id = outbound.new_message_id(self.owner)
        self.sent_ids.add(message_id)
        self.state.routes[message_id] = route
        entry = OutboxEntry(
            message_id,
            route,
            in_reply_to,
            subject,
            body,
            list(references or []),
            auto,
            trailer=trailer,
        )
        # Saved with its content before sending: a retry never reruns the agent.
        self.state.outbox.append(entry)
        self.state.save()
        await self.deliver(entry)

    async def deliver(self, entry: OutboxEntry) -> None:
        message_id = entry.planned_rfc_id
        task = self.deliveries.get(message_id)
        if task is None:
            if entry.state in {"sent", "failed"}:
                return
            task = asyncio.create_task(self._deliver(entry))
            self.deliveries[message_id] = task
            task.add_done_callback(lambda _: self.deliveries.pop(message_id, None))
        await asyncio.shield(task)

    async def _deliver(self, entry: OutboxEntry) -> None:
        if entry.attempts >= SEND_ATTEMPTS:
            entry.state = "failed"
            self.state.save()
            return
        message = outbound.compose(
            owner=self.owner,
            alias=self.alias,
            subject=entry.subject,
            body=entry.body,
            trailer=entry.trailer,
            in_reply_to=entry.in_reply_to,
            references=entry.references,
            auto=entry.auto,
            message_id=entry.planned_rfc_id,
        )
        entry.attempts += 1
        # In flight: a retry pass must not send it a second time meanwhile.
        entry.state = "sending"
        try:
            await asyncio.to_thread(self.mailbox.send, message)
        except Exception as error:  # noqa: BLE001 - retried, and shown here.
            if entry.attempts >= SEND_ATTEMPTS:
                entry.state = "failed"
                self.log(
                    f"email: gave up sending a reply after {entry.attempts} attempts: "
                    f"{error_message(error)}"
                )
            else:
                entry.state = "pending"
                entry.retry_at = self.clock() + SEND_BACKOFF * 2 ** (entry.attempts - 1)
                self.log(f"email: sending a reply failed ({error_message(error)}); will retry")
        else:
            entry.state = "sent"
        self.state.save()

    async def retry_outbox(self, *, force: bool = False) -> None:
        """Resend failed replies whose backoff has passed (all of them with `force`)."""
        for entry in self.state.outbox:
            if entry.state == "pending" and (force or entry.retry_at <= self.clock()):
                await self.deliver(entry)
