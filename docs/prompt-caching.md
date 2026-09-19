# Prompt caching and plan reminders

Pcode keeps plan reminders append-only because a reusable cache prefix must stay
in place as the conversation grows. An unchanged reminder string is not enough
if it disappears from its old position on the next request.

This explains the planning fix in `e28791b`. It does not claim that all cache
misses come from planning or that stable requests guarantee server-side hits.

## Why the upstream guarantee was not enough

There are two upstream designs to distinguish. When investigated, the
[public Planning documentation](https://pydantic.dev/docs/ai/harness/planning/)
described a breakpoint after the reminder's stable opening tag. Our pinned
Harness source at `12bce878da99bca61a5d8d798bff0a3bc93bd153` already included a
correction to that design. Treat the versioned source, not the live website, as
the description of what this installation does.

### Earlier design: a breakpoint after a moving tag

The website described an ephemeral reminder added to the per-request copy of
history, with a cache breakpoint between its opening tag and mutable plan text:

```text
Request 1: [history A] [<plan-reminder>] CACHE [plan]
Request 2: [history A] [new response + tool results] [<plan-reminder>] CACHE [plan]
```

Although the tag is identical, the prefix through it is not. Request 2 puts new
conversation content where request 1 had the tag. The entry written through that
tag therefore cannot be reused as that same prefix. Moving changing text to the
tail avoids rewriting the system prompt, but removing and relocating that tail
still matters for caching.

### Pinned design: a durable but potentially distant breakpoint

Upstream [PR #833](https://github.com/pydantic/pydantic-ai-harness/pull/833), commit
`178fa842`, moved the breakpoint onto the last durable user content. Our pin
includes this change. In `planning/_capability.py`, `_anchor_cache_breakpoint`
searches backward for a `UserPromptPart`; it does not anchor on a `ToolReturnPart`.
The temporary reminder is then appended in `wrap_model_request`.

```text
[user prompt] EXPLICIT CACHE [growing tool-loop history] [temporary plan]
```

The explicit prefix really is reusable now. However, during a long tool loop the
last user prompt can remain far behind the latest tool results. Protecting that
older prefix is not the same as making the entire growing conversation reusable.
Other capabilities that inject user parts can affect where the anchor lands.

### Interaction with Anthropic automatic caching

Pcode also enables Anthropic's automatic caching, which advances a breakpoint to
the last cacheable block:

```text
[user prompt] EXPLICIT CACHE [tool-loop history] [temporary plan] AUTOMATIC CACHE
```

On the next request, the old user-prompt prefix still matches. But the previous
automatic entry includes a reminder that has disappeared from its old position.
That entry no longer matches the growing conversation, so the tail can be written
again instead of reused.

Saved usage showed this pattern: cache-read tokens stayed fixed across long
sequences of requests while cache-write tokens grew with the conversation.
Request fingerprints also showed the previous temporary tail disappearing.
This supports the planning explanation; it is not evidence that every miss has
the same cause. Cache writes are not free reads: see Anthropic's current pricing
in its [prompt caching guide](https://platform.claude.com/docs/en/build-with-claude/prompt-caching).

## Pcode's fix: append only when the plan changes

[`IdentifiedPlanning`](../src/pcode/planning.py) appends a durable snapshot in
`before_model_request`, whose messages are persisted, and bypasses upstream's
ephemeral planning wrapper for every provider:

```text
Request 1: [history] [plan revision 1]
Request 2: [history] [plan revision 1] [new response + tool results]
Request 3: [history] [plan revision 1] [new response + tool results] [plan revision 2]
```

Earlier content stays in place. Unchanged plans add no reminder; changed plans
append an update that supersedes earlier snapshots. Clearing a previously shown
plan appends an explicit empty-plan update. Deduplication reads the current
branch's saved metadata, not a process-local flag, so it works across resume and
branch changes. The legacy `pcode_meridian_reminder` metadata key is retained for
compatibility with existing sessions.

The tradeoff is retaining old plan revisions. Appending only changes limits the
extra context, but the latest plan can become distant during a long tool loop.
The model can use `read_plan` to retrieve it. This fix does not add a new compaction
policy, usage accounting, request budgets, or delegated-agent cache settings.
Other providers' ephemeral limit-warning behavior is also unchanged.

## Codex and verification limits

The old planning mutation also applied to `openai-codex`. Pcode disables explicit
cache breakpoints for that route; reuse is server-managed. Append-only history
removes this source of prefix changes without requiring explicit marker support.
The [OpenAI caching guide](https://developers.openai.com/api/docs/guides/prompt-caching)
explains prefix reuse, but public API pricing and retention rules do not establish
Codex subscription quota accounting or guarantee hits on that endpoint.

Regression coverage checks request structure, not simulated cache-hit counts:

- [`test_prompt_cache.py`](../tests/test_prompt_cache.py) checks Anthropic cache
  controls reach the wire.
- [`test_meridian_reminders.py`](../tests/test_meridian_reminders.py) checks
  Anthropic and Meridian message-prefix stability across plan changes, clearing,
  and saved resume. Separate warning cases preserve the existing route-specific
  behavior.
- [`test_planning_cache.py`](../tests/test_planning_cache.py) checks native Codex
  wire-prefix stability, unchanged-plan deduplication, and branch-history handling.

Live confirmation requires observing cache reads advance with the conversation
while writes mostly track new content, rather than repeatedly tracking the whole
tail since the last user prompt. A passing wire test does not prove provider-side
reuse. See the [README's cache diagnostics](../README.md#prompt-cache-warnings)
for fingerprint dumps and warning interpretation.
