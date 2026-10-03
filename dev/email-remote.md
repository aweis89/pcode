# Email remote control: implementation plan

`pcode --email-listen` turns a Gmail account into a remote control for pcode:
a new email starts a session, a reply continues it. This revises the earlier
"Pcode Email Remote Control" spec: its security boundary stands, its
persistence and sync layers are cut down to what one owner needs, and the
execution side is pcode's existing session hosts rather than a new queue.

Read first: `src/pcode/host.py`, `src/pcode/remote.py`, `src/pcode/remote_print.py`,
`src/pcode/host_protocol.py`, `src/pcode/extensions/sandbox.py`, `src/pcode/sandbox.py`,
and `AGENTS.md` (testing traps). Nothing below asks for a new agent runtime, queue,
or plugin framework.

## Interaction

```text
pcode --email-listen            # in the workspace to expose
Launcher email arrives.
Reply with a task        → new session (host) in its own worktree
Keep replying            → continues that session
"Start new task" mailto  → another independent session, same alias
/status, /stop in a body → control, never a model turn
```

Terminal output on start: account, workspace, execution profile, expiry.
Never print the alias or token. Email-started sessions are ordinary hosts, so
`pcode --hosts` lists them and `pcode --attach <id>` takes one over locally;
say so in the launcher email.

## Phases

Do them in order. Phases 1 and 2 are independent of each other and both
precede any unattended tool execution.

### Phase 1: prove the Gmail assumptions (no execution)

A throwaway script, not shipped code, that answers with evidence:

- A reply from Gmail web and from the Gmail mobile app to
  `owner+pcode-<token>@gmail.com` arrives as **one message carrying the
  message-level `SENT` label** plus `INBOX`.
- A message delivered from outside with a forged `From: owner@gmail.com` to
  the alias does **not** carry `SENT`.
- The plus-address survives the round trip in the parsed `To` header.
- Our generated `Message-ID` on sent mail is what Gmail stores, and the
  reply's `In-Reply-To`/`References` point at it.
- Reply from the mobile app to a message whose `Reply-To` is the alias goes
  to the alias (not to `From`).

Run it over **both** transports and pick one from the result:

| | Gmail API (spec's choice) | IMAP + SMTP |
|---|---|---|
| Credential | OAuth, `gmail.readonly` + `gmail.send` | App password: whole mailbox, read/write/send |
| Setup | GCP project, Gmail API, Desktop OAuth client; Testing-mode refresh tokens expire every 7 days | 2-step verification + app password, nothing else |
| `SENT` check | `labelIds` on `users.messages.get` | `FETCH (X-GM-LABELS)` → `\Sent` |
| Stable message id | `id` | `X-GM-MSGID` |
| Candidate search | `q=to:<alias> newer_than:1d` | `SEARCH X-GM-RAW "to:<alias> newer_than:1d"` in `[Gmail]/All Mail` |

Prefer the API if the weekly re-login is acceptable; otherwise IMAP. Record
the choice and the evidence in this file. If the `SENT` property does not
hold on real mail, stop: do not relax the check to make a mock pass.

### Phase 2: remote execution profile

Email-spawned hosts run unattended, and pcode has no approval step. A host
started by the listener gets a fixed, locally chosen profile that cannot be
changed by email. Implement as options on `spawn_host`/`pcode.host` (a
`--remote` flag or similar), tested without any email code:

- `sandbox` extension forced on for the host (ignore `extensions_off`); the
  policy's deny-read list gains the listener state directory and the
  credential file. Writes stay confined to the worktree as the policy already
  does.
- Child environment scrubbed: build it from an allowlist (`PATH`, `HOME`,
  locale, the provider API keys pcode itself needs), not from the listener's
  full environment. `spawn_host` passes everything through today.
- MCP servers off for these hosts unless a preference opts them in; they run
  outside the sandbox.
- Per-turn limits: wall clock (30 min default), model requests and tool calls
  (100 each), counting sub-agents. Hitting one ends the turn with a visible
  status; nothing is silently truncated.
- Always `--worktree`; never the shared checkout. The worktree is left in
  place for local inspection; nothing is auto-merged.
- Project extensions and `worktree-setup` run only if the repository is
  already trusted (`project_trust`); the listener never answers that prompt.

The profile is printed at listener start and confirmed once during setup.

### Phase 3: `pcode.gateway` helper

Extract the "attach, submit, collect, detach" logic in `remote_print.py` into
a small module any non-terminal client can use. `--print` keeps working on top
of it. Surface:

```python
async def start_session(workspace, *, profile, resume=None) -> HostEntry
async def send(entry, text) -> TurnResult        # waits for the turn to end
async def status(entry) -> SessionStatus
async def stop(entry) -> None                     # cancel the turn + pending queue
```

`TurnResult` carries the reply text, how the turn ended (completed, failed,
cancelled, limit), and a change summary (worktree, branch, `git diff --stat`).
Starting a session whose host idled out resumes it (`--resume <session_id>`).
Test against the offline preview model with a real host; no email involved.

### Phase 4: email transport

`src/pcode/email_remote/` (listener, mailbox client, parsing, state). The CLI
flag lives in `app.py`; extensions cannot add flags or run background work.

**Listener lifetime.** Fresh 16-byte token per invocation, Base32 lowercase,
alias `owner+pcode-<token>@gmail.com`. TTL `--email-ttl 8h`. Store only the
token hash; the raw token lives in process memory and in delivered mail. On
exit or expiry: stop polling, cancel pending inputs, `stop` every running
session, keep transcripts and worktrees. A new invocation never drains or
resumes an old one's queue.

**Credentials.** In the macOS Keychain (`security` CLI) or an equivalent
store; a missing store is a setup error, never a plaintext fallback. Read by
the listener only; never exported to hosts (phase 2 scrub).

**Accept a message only when all hold**, checked before any body is read by
anything other than the parser:

1. Listener active and unexpired.
2. Parsed top-level `To` contains the exact live alias (not Cc/Bcc, not the
   body, not `Reply-To`).
3. Exactly one `From` mailbox and it equals the bound owner address; display
   names ignored; any `Resent-*` or conflicting sender rejects.
4. The message itself carries `SENT`.
5. Not pcode-generated (our recorded outbound IDs and marker header), not
   `Auto-Submitted` other than `no`, not a delivery report or list mail.
6. Gmail message id not already handled; routing unambiguous.

Rejected mail gets no reply and never reaches the model. Log a reason code.

**Routing** by RFC ancestry, never `threadId` or subject:

| Input | Action |
|---|---|
| No reply ancestry | New session |
| Ancestry resolves to one session | Continue it |
| First task reply to the launcher (or an unbound `/status` reply) | Create a session and bind that root; two rapid replies bind to the same one |
| Ancestry unknown | Reply (authenticated owner only) asking for a fresh composition; do not execute |
| Ancestry spans sessions | Reject, reply with an error |

**Outbound.** `From: pcode <owner@gmail.com>`, `To: owner@gmail.com`,
`Reply-To: alias`, `In-Reply-To`/`References` set, subject preserved,
`Auto-Submitted: auto-replied` (launcher: `auto-generated`), a marker header,
and a "Start new task" `mailto:` to the alias with no reply headers. Plain
text plus simple HTML; no ANSI, no raw transcript. Recipients are never taken
from incoming headers or model output. Send one ack when a session starts
(session id, worktree, `pcode --attach` hint) and one message when the turn
ends; coalesce for fast turns. The transport adds headers and footer; the
model never sees the alias.

**Body.** Prefer `text/plain`; else text from HTML without fetching anything.
Strip top-posted Gmail quoting and signatures conservatively (fixture-tested;
do not blanket-strip `>` or `--` lines). Empty new text, quoted-only, or
malformed MIME → ask for a clean resend, no execution. Attachments ignored
and mentioned in the reply. Limits: 64 KiB new text, 2 MiB fetched MIME.
Redact the alias from model-facing text. Subject may become the session
title.

**Controls.** `/status` and `/stop` only when the whole new body equals the
command. They go through `gateway.status`/`gateway.stop`, never the model.
`/stop` needs a resolved session; a fresh `/stop` gets an explanation.

**State** (`<state dir>/email-remote/`, `0700`, one JSON file rewritten
atomically; SQLite only if this proves inadequate):

```text
listener:   token_hash, owner, workspace, profile, started, expires
handled:    {gmail_message_id: disposition}
routes:     {rfc_message_id: session_id | "launcher" | "control"}
outbox:     [{planned_rfc_id, session_id, in_reply_to, body, attempts, state}]
```

Rules that keep the properties the spec cared about: record a handled id
before dispatching; record an outbox entry (with its content) before sending
so a retry never reruns the agent; bound retries and show failures in the
terminal. Poll every 5 s with jitter and backoff; the handled set makes the
`newer_than:1d` search idempotent, so no history cursor in v1.

**Limits** (preferences, listener-wide): 2 concurrent sessions, 20 sessions
per invocation, 20 pending inputs per session, 100 per listener. Over a limit:
refuse with a reply, never drop silently.

### Phase 5: only if needed

Gmail history cursors, SQLite, milestone emails for long turns, launchd unit,
a second transport behind `pcode.gateway`.

## Tests

Unit and mocked-mailbox tests for everything deterministic; real-account
tests opt-in via an env var and never committed with account data.

- Lifecycle: fresh alias per run; expiry stops intake and sessions; old
  listener's queue is not resumed.
- Auth: each of the six checks failing alone rejects; `SENT` on a sibling
  message does not qualify; forged `From`, wrong alias, stale token.
- Loops and destinations: our own mail, auto-replies, reports, list mail
  never trigger; incoming `Reply-To`/Cc and model output cannot redirect.
- Routing: two fresh emails → two sessions even with equal subjects; changed
  subject keeps the session; launcher binds once under two rapid replies;
  conflicting ancestry rejects.
- Parsing fixtures: web/mobile plain and HTML replies, quoted code, signatures,
  empty, oversized, attachment-only, quoted `/stop`.
- Profile: a host started with the remote profile cannot read the state dir or
  credential, has no inherited secrets in its environment, hits each limit
  visibly, and writes only inside its worktree.
- Gateway: start/send/status/stop against a real host on the offline model,
  including resume after the host idles out.
- Outbox: send failure retries the saved content without a new turn.

Real Gmail release checks from phase 1, re-run before enabling execution,
with the clients tested recorded here.

## Non-goals for v1

Per-thread aliases, token rotation, subject-based routing, Gmail-thread
routing, mailbox modification (labels, read state), emailed approvals or
privilege changes, attachments, multiple accounts or users, arbitrary remote
workspaces, a hosted relay or public endpoint, Slack/other transports.
