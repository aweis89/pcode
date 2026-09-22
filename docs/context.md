# Context, limits and caching

## Where the fixed prompt goes

`ctx:` is one number, which does not say why it is that large. `/status` also
breaks down the **prompt overhead**: the instructions and tool
schemas the provider is re-sent on every request, whatever the conversation did.

```
Prompt overhead           ~7.4k tokens · 3% of 200k · estimated
  Instructions            ~4.3k
    ~/AGENTS.md           ~2.8k · instructions
    AGENTS.md             ~675 · instructions
    Harness base prompts  ~228
    Planning tool         ~162
    Assistant config      ~105 · paths only · 1 skill
    File tools            ~102
    Sub-agents            ~90
    Web research          ~74
    Tool output limits    ~60
    Terminal instructions ~15
  Tool schemas            ~3.2k · 16 tools
    Largest               write_plan 625 · grep 314 · edit_file 310 · shell 259
```

Each repository instruction file gets its own row, so it is obvious when a global
`AGENTS.md` costs more than everything else combined. `Assistant config` is the
discovery block: **skills cost a path, not a body**, because pcode passes their
location and the model reads `SKILL.md` with a tool only when the skill runs. See
[Skills as slash commands](workspace.md#skills-as-slash-commands).

The rows are read from the last request's resolved instructions and tool
definitions, not re-derived, so they describe what was actually sent. Before the
first request there is nothing to attribute and the row says so. Token counts are
the same 4-characters-per-token estimate compaction uses, so they are comparable
with its threshold rather than exact provider counts.

## Context compaction

`/compact` makes a tool-free LLM call using the current model/provider credentials.
Optional instructions add focus without replacing the standard continuation summary:

```text
/compact
/compact Preserve auth debugging findings, exact file paths, and failing tests
/autocompact on
/autocompact off
```

The summary preserves goals and constraints, decisions, current state, exact artifacts,
verification results, and next steps/blockers. Recent messages are retained verbatim
with a token budget (up to 20k, scaled down for smaller windows); a single oversized
settled tool batch is summarized too rather than splitting its call/result pair.
Repeated compaction updates the previous summary. Summarizer tool-result input is
capped at 16k characters per result rather than Harness's default 500 characters.
Summaries are lossy: pre-compaction tool results remain available through the
session/tool history (including spill handles for reduced results), and the model
should retrieve spilled output or re-read source files when exact details matter.

Manual compaction requires an idle live session. Ctrl+C cancels it; queued prompts
wait until it finishes and are cleared on cancellation/failure. Short histories are
a no-op. Empty, invalid, or non-shrinking summaries are rejected without changing
active history. The result shows estimated before/after tokens; the context indicator
uses `~` until a new provider response supplies a measured count. Summary requests
contribute to session usage totals, not the completed-user-turn count.

Each successful manual compaction adds a selectable `/tree` checkpoint on the current
branch. It survives restart immediately, even without a subsequent prompt. Original
checkpoints, sibling branches, plan IDs/state, transcript, and tool-effect records are
retained. Navigating to a compaction checkpoint never runs a model or replays tools.
Unsaved sessions keep the same checkpoint in memory. Compaction is not deletion or
redaction of the saved conversation.

Automatic compaction is **on by default**. `/autocompact off` saves a user preference
in `~/.config/pcode/preferences.json` (or `$XDG_CONFIG_HOME/pcode/preferences.json`).
When enabled, pcode checks before every model request, including inside tool loops,
using provider usage plus estimated new input/tool results and tool schemas. It
triggers around 90% of the deployment window, reserved further for the
resolved output-token ceiling on smaller windows or large max-output settings.
Automatic summaries are persisted as safe checkpoints of the current run before the
next request. If compaction cannot make enough room, the run stops with an error;
it does not loop over summaries, silently drop history, or replay completed tools.
There is no automatic retry of provider context-overflow errors in this version.

Model catalog windows are advisory. Unknown deployments skip automatic compaction;
enabling it interactively requires a known window or an explicit override. For a
custom proxy, gated model window, or incorrect catalog entry, set the actual limit:

```sh
PCODE_CONTEXT_WINDOW=128000 pcode --model your-provider:your-model
```

The override applies to both the status line and compaction in the current process,
including model switches; update it if you change deployments. It is capped by
known provider input/maximum-context limits. For Codex, a larger window is only
used explicitly when its metadata advertises that maximum; pcode does not opt into
long context just because a generic model catalog advertises it. A summary can still fail if the
existing history itself is too large for the summarizer request. Failure leaves the
source history available rather than falling back to destructive truncation.

Pcode removes Coder's default clearing of old tool results at 70% context usage so
that evidence is not discarded before the summarizer sees it. With auto-compaction
off, use `/compact` proactively or `/new` for unrelated work.

## Model output limits

Anthropic requires a `max_tokens` ceiling for each response, including thinking
and tool-call arguments. Pcode resolves that ceiling from the serving model's
metadata on each request (also for delegated agents and after model switches).
Authenticated metadata takes precedence; public catalog limits are used only for
matching provider endpoints. If no output limit is known, pcode uses 16,384 tokens
instead of Pydantic AI's 4,096-token fallback. Explicit model settings take precedence,
and other providers keep their existing defaults.

This is a ceiling, not a requested response length or reasoning budget. Thinking
visibility and effort settings do not change it. The compaction summarizer retains
its separate, smaller output budget. Automatic compaction accounts for the resolved
ceiling, but reserves at most half the working window so small context overrides
remain usable. Provider limits still apply; truncation is not automatically retried.

## Tool output limits

Pcode uses [Harness ToolOutputLimits](https://pydantic.dev/docs/ai/harness/tool-output-limits/)
to reduce large results **once, before they enter model history**. By default, a
result of 10,000 characters or more is stored on disk; the model receives a handle
and a 1,000-character head/tail preview, plus a small retrieval header. Smaller
results pass through unchanged. This replaces Coder's 64,000-character truncation
and applies to the main agent and worker, including web/MCP tools and delegation
results. It makes no extra LLM calls and does not require automatic compaction.

```sh
pcode config set tool_output_mode spill          # Default: store, preview, read back
pcode config set tool_output_threshold 8000      # Trigger at 8,000 characters
pcode config set tool_output_preview_chars 800   # Content preview, excluding headers
pcode config set tool_output_max_chars 3000      # Fallback if storing fails
pcode config set tool_output_strategy head_tail  # Truncation keeps both ends
pcode config set tool_output_retention_hours 168 # Optional: prune spills older than a week

pcode config set tool_output_mode truncate       # Lossy, no new spill files
pcode config set tool_output_mode off            # No new result reduction
pcode config unset tool_output_mode              # Restore default spill mode
```

These settings also work through `/config`, with tab completion and validation.
They are snapshotted when the agent is constructed; restart pcode to apply them to
an existing conversation. They do not rewrite oversized results already in history.
All budgets are characters, not tokens. Keep the preview and truncation budgets
below the trigger threshold to save context. Spill previews always show both ends;
`tool_output_strategy` applies only to truncation and the spill-failure fallback.

The model uses `read_tool_result(handle, offset, limit, from_end, pattern)` to
retrieve selected lines or literal substring matches. Readback is exempt from
reduction and bounded by Harness to 1,000 lines / 50,000 content characters per call.
Structured returns are stored as indented JSON for paging. For a single line longer
than the readback cap, the agent is also told how to read a character range from the
spill file with shell. Retrieval stays available in `off` and `truncate` modes so
older handles still work after resuming a saved session.

Spills live in `$XDG_STATE_HOME/pcode/tool-results` (default
`~/.local/state/pcode/tool-results`), under an owner-only directory shared by pcode
workspaces and runs. They contain **raw tool output**, not the terminal's redacted
projection, and are written even with `--no-save`. This is local storage, not an
isolation boundary or encrypted credential store. To avoid new spill files, use
`truncate` or `off`; that does not delete existing spills, sessions, or shell logs.
By default spills are kept indefinitely. A nonzero retention schedules best-effort
background pruning on new writes, based on modification time, not last access.
Pruning or deleting files can break old handles; the read tool then asks the model
to rerun the original tool. Reset retention to `0` to disable future pruning.

Spilling preserves the result received by the limiter, not data a tool already
omitted. File-read pagination and the shell's native 16 KB output-tail cap still
apply, even in `off` mode. The full command output remains in the shell log. Shell
PID/log/status handles are kept outside the reduction budget, including with tiny
budgets or head truncation. Reduced shell bodies are omitted from the inspection
projection when their clipped text no longer has reliable redaction context; the
live preview remains separate. Store failures fall back to lossy truncation.
LLM summarization, multiple size bands, and per-tool configuration are not exposed.

## Prompt cache warnings

For the planning-specific cache issue and why reminders are now append-only, see
[prompt caching and plan reminders](prompt-caching.md). `make cache-report`
summarizes how prompt caching actually performed in saved sessions.

Prompt-cache warnings and fingerprint collection are **off by default**. Enable
`debug` for the main agent and sub-agents through the existing configuration:

```sh
pcode config set debug on   # Or /config set debug on inside pcode
pcode config set debug off  # Default
```

Restart pcode or use `/reload` after changing this setting. With debug enabled,
Harness's [cache-bust monitor](https://pydantic.dev/docs/ai/harness/warn-on-cache-busts/)
shows a `Prompt cache miss` warning when cache reads drop below half of an
established prefix of at least 1,024 tokens. It includes the model, token counts,
and request fingerprint comparison, without guessing the provider-side cause.
Warnings survive redraw and saved-session resume; turning debug off does not
remove old warnings. They do not interrupt the run.

The monitor compares requests within each agent run, not across chat turns or
restarts. A sustained collapse warns once until cache reads recover. It stays
quiet if the provider never reports an established cache. This is an observation,
not proof of a prompt bug: compaction, prefix changes, or provider cache expiry
can all cause a miss. No prompt contents are included in the warning.

### Diagnosing a miss

The token counts alone cannot say *why* a prefix stopped matching, so pcode
fingerprints every model request and keeps a rolling window of the last few. When
a miss fires, the warning gains a one-line diagnosis and the window is written to
`~/.local/state/pcode/cache-diagnostics/` (`XDG_STATE_HOME` is honored):

```text
! Prompt cache miss
  provider/model: request 14: cached 3,712 vs ~10,752 established tokens.
  Request fingerprints unchanged; 2 messages appended (~10s gap). Cache-miss cause unknown.
  Request fingerprints: ~/.local/state/pcode/cache-diagnostics/20260919T035812-4821-step14.json
```

The comparison describes what pcode observed, not a proven cache-miss cause:

- **`Request fingerprints unchanged`** — the tracked instructions, tools,
  settings, and earlier messages match; new messages were appended. These are
  application-level fingerprints, not the final HTTP payload. The cause remains
  unknown; the measured gap alone does not establish cache expiry.
- **`Message N of M changed`** — something rewrote history in place, and the named
  index is the first one that moved. `Instructions changed`, `Tool definitions
  changed`, `Cache settings changed`, and `History shrank` cover the cases that sit
  ahead of, or instead of, a message edit.

The dump holds digests, sizes, part kinds, token counts and breakpoint positions
for each request in the window — never prompt text, which would otherwise leak the
file contents and command output the agent had read. Compare consecutive entries
to see exactly which message moved. Set `PCODE_CACHE_DIAGNOSTICS=off` to disable
the dumps, or to a directory path to write them elsewhere; the warning itself is
unaffected.
