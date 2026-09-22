# Sessions and recovery

Live conversations save automatically when the first model prompt is submitted.
Opening the app, using commands, or quitting without a prompt creates no session.

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
a session for writing. `/resume` opens a full-screen browser of saved conversations
in the current repository, including its linked worktrees (newest first, labeled
by their first prompt), or the exact workspace outside Git, with every
prompt and a truncated, rendered response for the selected session alongside. `/`
searches prompts across sessions (space-separated words are all required) and ↑/↓
move the selection while you type (Ctrl+U/Ctrl+D by half a page); `r` includes
responses, `w` includes every workspace. Tab focuses the content pane, where arrows
scroll by line, PageUp/PageDown by page, and Ctrl+U/Ctrl+D by half a page. Enter resumes the selected
session in place, Esc cancels. Resuming restores the saved model, history, and plan.
A session from another worktree of the same repository switches the workspace to
that worktree: file tools, the shell, extensions, and skill commands are rebuilt
there, and the worktree being left is tidied as on exit (an untouched `pcode-`
worktree is removed; unmerged work is kept with a note). Sessions from another
repository are refused.

## Recalling earlier sessions

The bundled `session_history` extension lets the model answer questions such as
“What did we decide about editor flicker?” without resuming another session.
`search_sessions` returns ranked excerpts with session/turn IDs, dates, outcomes,
and active/inactive branch labels. `read_session` retrieves a referenced turn,
paginates long text, and includes bounded ancestor context (not sibling branches).

- Default `scope="project"` includes linked worktrees. `workspace` restricts to
  the exact directory; `all` is for explicitly cross-project questions.
- `scope="session"` searches the current conversation, including original turns
  dropped from model context by compaction. It reads the journal without taking
  the live session's lock. This is not a replacement for model checkpoints.
- Search covers saved prompts, consumed steering messages, assistant text, and
  tool summaries/commands, not full tool results, reasoning, or unsaved conversations.
  Steering messages from older versions were not journaled and are not recalled.
  Historical claims and
  failed or abandoned attempts are evidence, not proof that a change shipped.
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
Each search has a 64 MiB journal-read budget, returns candidates from at most
10,000 chunks, and embeds at most 128 new chunks. Sessions are scanned newest
first, each journal from its beginning. Reaching a limit reports partial coverage;
branch labels are unknown when the rest of a journal was not read. Narrowing to
`scope="session"` avoids spending the budget on other conversations. Reading a
referenced turn also has a 64 MiB scan budget. Subsequent semantic searches extend
the vector cache (they do not advance the journal-read window). Cache
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
to remove it; there is no automatic retention policy yet.

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

Use `/resend` while idle to try again manually without adding another user
message. The original prompt appears above the task bar with the normal running
spinner. Completed tool results stay in context; if the last answer completed,
only that final response is regenerated. An empty conversation cannot be resent.
If cancellation left tool effects unsettled, `/resend` refuses to replay them;
inspect the tools and send an explicit next step instead.

Nothing from sessions run before this feature was installed can be reconstructed
from disk; those earlier conversations were memory-only.
