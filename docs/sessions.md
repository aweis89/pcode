# Sessions and recovery

Live conversations save automatically when the first model prompt is submitted.
Opening the app, using commands, or quitting without a prompt creates no session.

## Background sessions

Every interactive session runs in a *session host*: a headless pcode process
that owns the conversation (the agent, its tools, the journal) while the
terminal only draws it. The terminal can then leave, switch to another session,
or close, and the work carries on. `--no-host` (or `session_host off`) runs a
session inside the terminal instead, as `--print` does unless it is given
`--attach` (see [Scripting a running host](#scripting-a-running-host)).

```sh
pcode                            # start a session in a host and attach to it
pcode --no-host                  # run this one inside the terminal instead
pcode --hosts                    # list running hosts
pcode --attach                   # reattach to the newest host in this repository
pcode --attach 3f9c              # ...or to one by host or session ID prefix
pcode --stop-hosts stale         # stop hosts still running older pcode code (or: all)
```

Inside a hosted session:

- `/switch` opens a picker over every running host, with what each one is doing.
  Enter shows that session in this terminal; `n` (or Ctrl+N) starts a new one;
  `x`, pressed twice, stops one. A turn you switch away from keeps running.
- `/switch HOST` goes straight to one by host or session ID prefix, and
  `/switch -` (or Ctrl+^, which most terminals also send for Ctrl+6) back to the
  one this terminal showed before. Pressed again, it flips back.
- `/switch new` starts a new session and switches to it. `/switch new PROMPT`
  starts one working on PROMPT and leaves it in the background; this terminal
  stays where it is.
- `/resume` resumes a saved conversation in a host of its own, or shows it where
  it is already running.
- `/restart` restarts this session's host on the pcode installed now and picks the
  conversation up again in the same worktree. A host keeps the code it started
  with, so this is how a long-running session gets a fix you just merged; the
  picker and `--hosts` mark hosts that are on older code.
- `/stop` ends this session's host and quits, asking about the worktree the way a
  local exit does. Plain quitting (Ctrl+D, `/quit`, or closing the terminal)
  only detaches.

### Scripting a running host

`--attach` with `--print` sends one message or command to a running host
without opening the editor, then detaches. The host keeps running, and any
terminal attached to it sees the turn as usual.

```sh
pcode --attach 3f9c -p "Run the tests and summarize failures"   # reply on stdout
pcode --attach 3f9c -p /compact                                  # a session command
pcode --attach 3f9c -p /stop                                     # end the host
```

`--attach` takes an optional HOST, so it swallows the word after it: write
`pcode --attach -p "..."` or `pcode --attach 3f9c "..." -p`, never
`pcode -p --attach "..."`, which reads the message as a host ID.

- A message waits behind whatever the host is already doing, then runs as its
  own turn, never mixed into one already running. That turn is printed: the
  reply on stdout, tools and notes on stderr, as for a local `--print` (notes
  another terminal causes meanwhile appear there too). The exit status says
  whether the turn succeeded. It fails at once if the message is dropped
  before running, because the host's queue was cleared (a Ctrl+C in another
  terminal, or a turn ahead of it failing).
- The message is always sent to the model: a leading `!` does not make it a
  shell command, as it would typed into an attached terminal.
- A session command (`/compact`, `/effort high`, `/model NAME`, ...) runs in the
  host, and pcode exits once it has, including compaction or MCP work it started.
  It exits non-zero if the command did not run (its queue was cleared) or
  reported an error or a warning, such as `/compact` refused while a turn runs.
  Anything else the host reports meanwhile counts too. A command that opens a
  picker (a bare `/model`) fails, since nobody is there to pick. Commands that
  queue a turn of their own (a skill, `/resend`) exit without waiting for it. A
  command that ends the session (`/worktree finish`) succeeds; a host stopped
  under a command by anything else is a failure.
- `/stop` ends the host, which tidies its own worktree the way `worktree_exit`
  says, without asking, since nobody is there to be asked. Its notes about that
  go to the host's log. Other terminal commands (`/tree`, `/switch`, ...) need
  the editor and are refused.
- Ctrl+C only detaches (exit status 130): the message's turn keeps running, or
  stays queued, in the host. Stop it from an attached terminal, where Ctrl+C
  also clears the host's queue.
- The session's own settings apply: `-m`, `--no-save`, `--worktree`, and
  `--continue` are ignored. (`pcode --continue ID -p ...` without `--attach`
  still runs a copy of the session locally, even while a host runs it.)
- The host must be running this version of pcode or later. An older one
  refuses with a message saying so, and `/restart` in an attached terminal
  updates it.

The footer counts the other running sessions, how many are working, and how
many finished while nobody was looking (*new*; the picker lists those first).
When one of them finishes a turn a note appears here, and if no terminal is
showing that session, a desktop notification too: pcode asks the terminal to
raise it (OSC 9, which Ghostty shows by default), once per turn however many
terminals are open. While a turn runs, the tab shows a progress indicator
(OSC 9;4). `pcode config set desktop_notifications off` turns both off.

A host with no terminal attached, no turn running, and no running command
stops itself after an hour (`session_host_idle_minutes`; `0` never stops).
Nothing is lost: `/resume` or `pcode --continue` brings the conversation back.

### How it works

Each host is its own process, started by the terminal that asked for it, so it
inherits that terminal's environment (direnv credentials, `PATH`, tool
versions) exactly as a local session would. One session crashing or hanging
does not touch the others. A host listens on a Unix socket; a terminal that
attaches is sent the conversation so far and then every event as the turn
produces it, so switching to a session mid-turn picks the turn up where it is,
streaming text and running commands included.

A new host started with the `worktree` setting on makes its own worktree, the
same as a local session, and `/switch new` starts from the main checkout so the
new session never shares yours. A host tidies its worktree when it stops, as a
local session does on exit, without asking: unmerged work is kept with a note in
the host's log.

Hosts keep running until stopped. Their sockets, status files, and logs live in
`~/.local/state/pcode/hosts/` (`PCODE_HOST_DIR` overrides it; a Unix socket path
is limited to about 100 bytes, so keep it short).

### What works in a hosted session

Everything. The host runs the session's commands (`/model`, `/effort`, `/compact`,
`/resend`, `/new`, `/tree`, `/btw`, `/mcp`, `/jobs`, `/worktree`, `/login`,
`/reload`, skills, and extension commands), and opens their pickers in the
terminal that typed them. The terminal runs its own (`/switch`, `/resume`,
`/status`, `/tools`, `/diffs`, `/links`, `/workers`, `/help`, `/config`, and the
display commands), reading the conversation from the host's journal on disk.
MCP sign-ins that need a browser open it from the host, on the same machine.

A host started by an older pcode keeps running that code until it stops. A
terminal on a different protocol version is refused with a message saying so.

## Resuming

```sh
uv run pcode --sessions
uv run pcode --continue                             # this directory's newest session
uv run pcode --continue SESSION_ID
uv run pcode -m openai-codex:gpt-5.6-sol --no-save  # opt out for a sensitive session
```

`-c` / `--continue` accepts an unambiguous ID prefix (at least 8 characters) and
restores the saved model, workspace, and structured message history. Without an ID
it picks the newest session whose workspace is the current directory (or `-C`), not
the newest session overall. It rebuilds the retained transcript using the same
redraw path and `transcript_max_chars` budget as live scrollback (default 2,000,000
characters), then waits for your next message; it never re-runs tools. This replaces
the terminal's screen and scrollback, just like `/redraw`. See
[transcript regeneration](transcript.md#regenerating-the-terminal-transcript).
A different explicit `-m` is rejected on resume, as is a `-C` in another
repository; `-C` pointing at another worktree of the same repository is fine and
the session goes back to its own directory. Only one process may open
a session for writing; continuing one that is already open
[continues a copy](#continuing-a-session-that-is-open-elsewhere). `/resume` opens a full-screen browser of saved conversations
in the current repository, including its linked worktrees (newest first, labeled
by their first prompt), or the exact workspace outside Git, with every
prompt and a truncated, rendered response for the selected session alongside. It
opens in the search line: typing searches prompts across sessions (space-separated
words are all required) and ↑/↓ move the selection while you type (Ctrl+U/Ctrl+D by
half a page). Tab moves to the session list, where `/` returns to the search, `r`
includes responses, and `w` includes every workspace. Tab again focuses the content pane, where arrows
scroll by line, PageUp/PageDown by page, and Ctrl+U/Ctrl+D by half a page. Enter resumes the selected
session in place, Esc cancels. `d` (or Delete) in the session list, pressed twice,
permanently removes the selected session's directory; the active session and one
open in another process are refused. Resuming restores the saved model, history, and plan.
A session from another worktree of the same repository switches the workspace to
that worktree: file tools, the shell, extensions, and skill commands are rebuilt
there, and the worktree being left is tidied as on exit (an untouched `pcode-`
worktree is removed; unmerged work is kept with a note). Sessions from another
repository are refused.

### Continuing a session that is open elsewhere

`--continue` or `/resume` on a session that another pcode process has open,
even one in the middle of a turn, continues a copy of it instead of refusing.
The copy is a new session with its own ID, and the transcript opens with a note
naming the original. The original is only read, never written, and keeps
running undisturbed.

A turn still running in the original is copied the way a crash would leave it:
the copy picks up from that turn's last settled step (or from the turn before,
if it has not settled one yet). `/resend` carries the turn on from there, a new
message starts from the same point, and `/tree` can go back to the last
finished turn instead.

The copy works in the same directory as the original, so if both edit files
their changes land in one checkout. Quitting either one does not remove or
merge a shared `pcode-` worktree while the other is still open in it.

A session still running in a [background host](#background-sessions) is not
copied: `--continue` and `/resume` show it where it runs instead.

### When the workspace was deleted

A session worktree can be removed by something other than the session that owns
it: a sibling session merging and removing it, `/worktree clean`, or `git
worktree prune`. The conversation is still resumable. `--continue` then
continues in an explicit `-C DIR` of the same repository, or else in the
checkout the worktree was made from, and says on stderr which directory it
switched to; `/resume` continues in the workspace you are already in. Only a
session whose repository is also gone is refused, and the message names the
directory it was looking for.

A workspace that disappears *during* a session is not recoverable in place: the
shell and file tools resolve every path against it, so the next tool call stops
the turn with a message naming the deleted directory, rather than retrying a
command that cannot succeed. Quit and continue the session elsewhere.

## Recalling earlier sessions

The bundled `session_history` extension lets the model answer questions such as
“What did we decide about editor flicker?” without resuming another session.
`search_sessions` returns ranked excerpts grouped by session, with session/turn
IDs, dates, outcomes, and active/inactive branch labels. `read_session` retrieves a referenced turn,
paginates long text, and includes bounded ancestor context (not sibling branches).

- Default `scope="project"` includes linked worktrees. `workspace` restricts to
  the exact directory; `all` is for explicitly cross-project questions.
- `scope="session"` searches the current conversation, including original turns
  dropped from model context by compaction — `/compact` between turns, and
  automatic compaction inside a long turn, which journals an `auto_compacted`
  marker so recall knows the running turn is no longer fully in context. It reads
  the journal without taking the live session's lock. This is not a replacement
  for model checkpoints.
- Search covers saved prompts, consumed steering messages, assistant text, and
  tool summaries/commands, not full tool results, reasoning, or unsaved conversations.
  Steering messages from older versions were not journaled and are not recalled.
  Historical claims and
  failed or abandoned attempts are evidence, not proof that a change shipped.
- Hits are grouped by session so one long session cannot take every slot: each
  session gets at most three turns until the limit would otherwise go unused.
  The current conversation is flagged `current`, and the turn running the search
  is never returned as evidence — except after automatic compaction has dropped
  part of that turn from context, when it comes back marked `current_turn`.
- Excerpts are centred on the densest match in prose where there is one, so a
  conclusion outranks the shell command that led to it, and each hit carries the
  turn's closing assistant text as `conclusion` when the excerpt misses it.
- Results report cumulative `sessions_searched` against `sessions_in_scope`, plus
  `sessions_partial`, `sessions_unreadable`, and `scan_complete`. Sessions start
  newest first, with traversal order fixed across pages. Both the journal-byte
  budget and the chunk limit return `next_cursor` when work remains. Pass it as
  `after` with the same arguments, even when the page has no hits. Continuation
  resumes inside the journal or turn, without skipping its remaining records or
  chunks. Coverage counts apply to the whole continuation chain; hits and rankings
  apply to the current page. An empty page does not establish absence while
  coverage is incomplete.
- A session's hits are deferred until its journal snapshot has been read in full,
  so attribution, branch labels, redaction, and text offsets agree. A journal
  larger than the byte budget can therefore produce several empty pages first.
  A session counts as searched only after all its chunks have been considered.
- `read_session` also returns `next_cursor` if it needs more journal bytes before
  resolving a turn. Continue with `after`, keeping the other arguments unchanged.
  Once `next_cursor` is null, use `next_offset` to page through the returned turn's
  text. A scan cutoff is not reported as a missing turn.
- Continuations retain parser state in memory, not a persistent transcript cache.
  Tokens are single-use, tied to the scope and request, and expire after 30 idle
  minutes or a process restart. At most 16 pending continuations are retained;
  the oldest is evicted when that limit is reached. An expired token reports an
  error: restart without `after`. Each journal is read to the size captured when
  first opened; appends require a fresh search. Replaced files, shrinking files,
  and same-size edits invalidate a continuation. Journals must otherwise remain
  append-only: a growing in-place rewrite is not reliably distinguishable from
  an append. An unfinished final record produces a warning rather than a claim
  of complete coverage.
- New sessions record their project path so deleted worktrees remain discoverable.
  Older sessions use Git discovery or the conventional `.worktrees/` layout;
  a deleted legacy worktree elsewhere may need `scope="all"`.

Keyword retrieval is BM25 (the same ranking as Harness's `ConversationSearch`)
with a bonus for an exact phrase match; it needs no credentials or network.
Optional hybrid retrieval uses
Pydantic AI's `Embedder` when you explicitly set a model before launching:

```sh
PCODE_HISTORY_EMBEDDING_MODEL=openai:text-embedding-3-small pcode
```

This opts into sending redacted chunks and queries to the selected embedding
provider, using that provider's normal credentials. Redaction is best-effort;
do not enable a hosted model for history you cannot send off-machine. Local
embedding models require their provider's optional dependencies. No model is
selected automatically, and `semantic=false` on a search forces keyword-only.
Provider/cache failures fall back to keywords with a warning.

The optional cache is `.history-embeddings.sqlite3` inside the session root,
mode 0600, containing content hashes and vectors, not transcript text. Vectors
are still sensitive data. It is created lazily; there is no startup indexing.
Each search call has a 256 MiB journal-read budget, returns candidates from at
most 10,000 chunks, and embeds at most 128 new chunks. Reading a referenced turn
has the same per-call byte budget. These limits bound journal bytes read and
chunks ranked, not total memory or processing time: an unfinished record and a
session's parsed turns are retained across calls, and a complete JSON record is
decoded as a unit. They do not cap the history reachable through continuation.
Narrowing to `scope="session"` avoids
spending the budget on other conversations. Subsequent semantic searches extend
the vector cache; use `after` explicitly to advance the journal scan. Cache
keys include the model and content; removed sessions are never returned, but
old cached vectors remain until the cache is deleted. Delete that file with
pcode stopped to clear it (also necessary if a provider changes a model's vector
dimensions under the same name).

To disable recall, create `~/.config/pcode/extensions/session_history.py` with
`def setup(pcode): pass`, then `/reload`. The tools honor the same session-storage
configuration as the terminal.

## Where sessions are stored

Default location: `$XDG_STATE_HOME/pcode/sessions`, or
`~/.local/state/pcode/sessions`. Override with `--session-dir PATH` or
`PCODE_SESSION_DIR`. Each session directory contains:

- `session.json`: model, workspace, timestamps, completed-turn usage, and package versions.
- `steps.sqlite3`: Harness `StepPersistence` events, full Pydantic message snapshots
  (including tool arguments/results and provider reasoning metadata), and a tool-effect ledger.
- `transcript.jsonl`: submitted prompts, streamed text, completed blocks/tool summaries,
  bounded redacted tool-inspection arguments/results, and structured failure diagnostics (HTTP status, provider code/parameter/message).
  Failed turns also record the configured parent model's provider and base URL
  when available, both in the transcript and `errors.log`. URL userinfo, query,
  and fragment are omitted. This is the configured route, not proof of which
  endpoint failed inside a delegated run. Quota/credit exhaustion and rate limits
  get specific guidance rather than the generic login/connectivity hint.

Session directories are mode 0700 and data files are 0600. **These files contain
conversation and repository content in plaintext.** They stay outside the repo by
default; do not commit or share them without inspection. No HTTP headers, provider
credential store, or auth tokens are deliberately captured. Error diagnostics
redact known token formats, credential assignments, and credential values from
the environment; this is best-effort, not a guarantee that arbitrary sensitive
text can be recognized. Model snapshots retain their content faithfully for replay,
so sensitive material pasted by you or returned by a tool can still be stored.
Use `--no-save` when that is inappropriate. Delete a closed session's directory
to remove it; conversations are never deleted for you.

### Disk use

`steps.sqlite3` holds all of it. Harness saves the whole message history again
at every settled step, so a turn with hundreds of tool calls would store
hundreds of copies of itself. Each turn keeps its newest two checkpoints
instead, which is what resume and `/tree` read; sessions written before that
bound existed keep every step and can reach gigabytes.

```sh
pcode --sessions --compact   # Drop superseded checkpoints, report space freed.
```

That rewrites each closed session's store in place, skipping any session open
in another process, and reports what it reclaimed. It removes no conversation:
every turn still restores from the step it settled at, so `--continue`, `/resume`
and `/tree` navigation to an earlier turn all work afterwards. Recall is
unaffected too: search reads `transcript.jsonl`, which `--compact` never touches. On the largest session
observed (1.3 GB, 338 checkpoints across 10 turns) it took under a second and
left 67 MB.

## Checkpoints

Checkpoints are saved by Harness at settled tool boundaries, not just when an
answer succeeds. If the request after a completed tool fails, its tool result is
retained for the next turn and for resume. Interrupted streaming text is retained
in the journal, even when it cannot become a safe model checkpoint. Resume uses
the most recent settled checkpoint without requiring review of interrupted tools.
Pending tool calls are not automatically replayed, and their unknown outcomes
remain in the diagnostic ledger. Interrupted tools may already have changed the
workspace; resuming does not undo those effects. Checkpoints do not restore files,
running processes, or capability-local state such as the in-memory planner.

Only a turn's newest checkpoint is read, so that is what is kept (plus its
newest settled one, when the newest is interrupted). Rewinding to an earlier
step *within* a turn is not offered; `/tree` moves between turns.

## Retries and `/resend`

Dropped provider connections and transport timeouts get one automatic retry by
default (two attempts total per submitted turn). The retry reuses the failed
request's checkpoint, including completed tool results, without adding a
"continue" prompt. Partial streamed output may remain visible but is excluded
from the retried request. Authentication, HTTP status errors, tool errors, and
user cancellation are not automatically retried by pcode. Anthropic SDK request
retries are disabled for all three authentication modes: rate-limit, billing,
and server errors surface immediately rather than waiting through hidden
backoff. Transport retries show the failure and attempt count. OAuth credential
refresh still handles an expired access token. Other provider SDKs may retry
internally. Use `/resend` to retry a failed request when ready.

```sh
pcode config set retry_attempts 3   # Three extra attempts per turn, next launch
pcode config set retry_attempts 0   # Disable automatic retries
```

One HTTP status error is retried, because repeating it unchanged cannot help.
Anthropic encrypts each server-side `web_search` result to the account that ran
the search, so a session resumed under a different login is rejected with
`Invalid encrypted_content in search_result block` — and the results are in the
history, so every later request fails the same way. pcode drops them, keeping
each page's title and URL and the model's own reading of the search, says how
many went, and sends the turn again. This costs nothing from the retry budget
above and happens once per turn; a rejection that survives it is reported.

A tool call whose arguments fail the tool's schema is a separate budget. The
model is told what was wrong and gets three corrections by default; past that
the turn ends, naming the tool and the rejected field rather than blaming the
provider. Nested arguments such as `edit_file`'s `replacements` array are the
usual cause, and the correction costs a round trip where the old limit of one
cost the turn. Output validation keeps the stricter single retry.

```sh
pcode config set tool_retries 5   # More corrections before a turn is abandoned
pcode config set tool_retries 0   # Fail on the first rejected tool call
```

`strict_tools`, on by default, also sends `edit_file` with Anthropic's
[strict tool use][strict] flag, which is documented to constrain sampling to
the tool's schema. In practice it does not stop this particular mistake: with
the flag confirmed on the wire, Anthropic still returns `replacements` as a
truncated string often enough that the measured failure rate barely moved
(11.5% to 9.6% of calls that use the array). The retry budget above is what
recovers the edit; the flag stays on only because nothing got worse.

```sh
pcode config set strict_tools off   # Leave edit_file arguments unconstrained
```

Nothing else changes: OpenAI models already infer strict mode, other providers
ignore the flag, and a model or schema that cannot support it is left alone
rather than failing. Anthropic rejects an entire request whose strict schema
uses a keyword it does not accept, so pcode only constrains schemas built from
a known-supported subset and silently skips the rest.

[strict]: https://platform.claude.com/docs/en/build-with-claude/structured-outputs

Use `/resend` while idle to try again manually without adding another user
message. The original prompt appears above the task bar with the normal running
spinner. Completed tool results stay in context; if the last answer completed,
only that final response is regenerated. An empty conversation cannot be resent.
If cancellation left tool effects unsettled, `/resend` refuses to replay them;
inspect the tools and send an explicit next step instead.

Nothing from sessions run before this feature was installed can be reconstructed
from disk; those earlier conversations were memory-only.
