# Bounded delegation waits: plan

Working notes for keeping a parent's prompt cache warm while it waits on
sub-agents. Nothing here is built yet. Update the checklist in the same commit as
the change.

## Why

`anthropic:` and `claude:` both write five-minute cache entries (see
[prompt caching](prompt-caching.md) and
[anthropic-providers](anthropic-providers.md#what-shipped)). An entry lives five
minutes from the *start* of the request that last read or wrote it, so the next
request has to start inside that window, generation time included.

Tool waits already fit. `shell` and `wait_for_job` hand back a job handle after at
most 270 seconds, and every saved `anthropic:` case (eight, 246–275 s apart)
reused the whole prefix. `delegate_task` has no such cap: Harness runs the child
inside the tool call and returns only when it finishes. Every tool-loop gap over
five minutes in the saved sessions is a delegation, 5 to 22 minutes long, and on
`anthropic:` the parent's next request reused 0%, 0% and 39% of its prefix. Before
the 5-minute switch, `claude:` hid this behind its one-hour writes.

A keep-alive ping (`max_tokens: 0`) cannot help `claude:`, because the CLI builds
those requests. The fix has to give the model a tool result before the window
closes, so the parent's next request is a real one on any provider.

## Decisions (proposed)

- `delegate_task` waits for its child only until a deadline measured from the
  start of the parent's last model request. A child still running then keeps
  running, and the tool returns a handle.
- Handles are job ids (`d1`, `d2`, ...). `wait_for_job`, `stop_job` and
  `job_output` accept them, so the model has one way to wait on background work.
- A finished child's result reaches the model at its next request, as job exits
  already do, or as the result of a `wait_for_job` on it.
- A detached child never outlives its parent run. The run does not end while one
  is running, and cancelling the run cancels it.
- The same deadline bounds `shell` and `wait_for_job`, replacing their fixed 270
  seconds from the tool's own start.
- It applies to every provider. The extra rounds are cheap everywhere, and one
  code path is easier to reason about than a per-route switch.

## How it works

**Deadline.** A small capability records `monotonic()` in `before_model_request`,
keyed by run id. The wait budget for any blocking tool in that run is
`request_start + 270 s - now`, floored at about 20 seconds so a long generation
does not turn into an instant empty round. 270 leaves 30 seconds for sibling
tools, hooks and the network before the next request starts. For `claude:` the
recorded start is when pcode hands the turn to the CLI, which is close enough.

**Detach.** `WorkspaceSubAgentToolset.delegate_task` starts the delegation
(Harness's `_run_delegation`, worker slot included) as a task, then
`asyncio.wait`s on it for the budget. Finished in time: return its result or
raise its exception, exactly as today. Otherwise the task goes into a per-run
registry under a new id and the tool returns:

```text
[d1] worker "fixing the /btw cache prefix" is still running (4m 10s). Its result
reaches you automatically when it finishes. To wait for it, call
wait_for_job("d1"); stop_job("d1") cancels it. Don't edit files it may be
changing while it runs.
```

The task is created inside `DelegationReporting.wrap_tool_execute`, so its copied
context keeps the `_parent` binding, and child activity keeps reaching the
original delegate row. Harness emits `DelegationEndEvent` through the tool's
`RunContext` when the child settles. `ctx.emit` writes into the run's event
buffer, so this works after the tool call returns, as long as the run is alive.

**Delivery.** When a detached child settles, its task calls
`ctx.enqueue(notice, priority="asap")` on the parent context, unless a
`wait_for_job` is currently waiting on it, in which case that call returns the
result directly. The notice carries the child's whole output, as the tool result
would have, so it costs no extra request to read:

```text
[d1] worker "fixing the /btw cache prefix" → done after 7m 12s. Result:
...
```

A child that failed (Harness's `ModelRetry` path) is delivered the same way, with
the steering text Harness would have raised.

**Run end.** A capability's `after_node_run` sees the graph's `End` before
pydantic-ai drains pending messages (`run.py`: `after_node_run`, then
`drain_pending_messages_at_end`). If the run has detached children still running,
the hook waits until one settles or the deadline passes, then enqueues either the
results or a status note (`[d1] still running (9m 40s); wait_for_job("d1") to keep
waiting`). The drain turns that into another model turn instead of ending the run.
This keeps the cache warm even when the model stops to "wait", at the cost of one
short round every four and a half minutes.

**Cancellation.** A `wrap_run` `finally` (covering Ctrl+C, errors and `RunCancelled`)
cancels and awaits the run's detached tasks, so none are left behind emitting into
a closed buffer. Isolated workers keep their existing cancelled-checkout handling
(`_finish(..., "cancelled", ...)`).

**UI.** `live.py` settles a delegate row on its `FunctionToolResultEvent`, and a
result with no `DelegationEndEvent` currently reads as "Not started". A result that
is a detach handle leaves the row running instead, and the later
`DelegationEndEvent` for that call id settles it with the usual
`delegation_detail`. `Workers` does the same (it marks a worker settled on the
delegate's `ToolSummary`). `Workers.end_turn()` needs no change, since a turn no
longer ends under a running child.

**`claude:`.** Nothing provider-specific. The handle releases the parked MCP
handler, so the CLI sends its next request. An enqueued notice beside tool results
is queued input the CLI sends with them. A run-end redirect is a new user message
on the live process.

## Cost

Each extra round is a real request: a cache read of the prefix plus a short
reply. On Opus 5.5 with a 150k-token prefix that is about 0.05 × 150k = 7.5k
input-token units plus a few hundred output tokens at 5x, against 1.25 × 150k ≈
190k to rewrite the prefix after an expired wait. A 20-minute delegation costs
four rounds, roughly a sixth of one rewrite.

## Seams

| Where | Change |
|---|---|
| New `pcode/wait_budget.py` | Capability recording each run's request start; `budget(ctx)` helper |
| `isolated_delegation.py` | Detach in `delegate_task`; per-run registry of detached tasks |
| `shell_tools.py` | `_wait_seconds` takes the deadline; `wait_for_job` / `stop_job` / `job_output` accept `d` ids |
| New capability (beside `JobNotices`) | `after_node_run` run-end wait and redirect; `wrap_run` cleanup |
| `live.py`, `workers.py` | Detached delegate rows stay running until `DelegationEndEvent` |
| `agent.py` | Install the capabilities on the main agent and `shared_capabilities` |

## Risks and open questions

- **Model behavior.** A model may start overlapping work while a shared-mode worker
  edits the same files, or end its turn early. The handle text and the run-end
  redirect cover the second; the first needs a live check on Opus 5.5 and a Codex
  model before shipping.
- **Harness internals.** This relies on `_run_delegation` emitting its end event
  through the tool's `RunContext` after the call returned, and on `after_node_run`
  running before the pending-message drain. Pin both with tests so an upgrade
  fails loudly.
- **Transcript noise.** Run-end status notes make a quiet wait visible as extra
  model turns. They should render like job notices, not like user messages.
- **Resume.** A pcode crash mid-delegation already loses the child. Now the history
  holds a handle with no result, so a resumed model may wait on a job that no
  longer exists. `wait_for_job` on an unknown `d` id should say the child was lost.
- **Parallel delegations.** Several `delegate_task` calls in one batch share the
  deadline, and `wait_for_job` calls on them can run in parallel. No new tool is
  needed for "wait for any".

## Tests

- A scripted child held open with `gate()`/`release()`, never `sleep`, and a
  deadline shrunk for the test: the tool returns a handle, the child keeps running,
  and its result arrives in the next request's parts.
- `wait_for_job("d1")` returns the result when the child settles during the wait,
  and the notice is not delivered twice.
- A model that ends its turn with a child running gets redirected, and the run
  ends only after the result is delivered.
- Ctrl+C with a detached child cancels it and leaves no pending task.
- The delegate row stays running across the handle and settles on the end event,
  in-process and over `make test-socket`.
- The deadline shortens `shell` waits after a long generation.

## Verification

After it lands, `cache_report.py` on a session with a delegation over five minutes
should show the parent's post-delegation request reading at least 75% of the
previous input on `anthropic:` and `claude:`, with the extra rounds visible as
short requests.

## Checklist

- [ ] Wait deadline capability, used by `shell` and `wait_for_job`
- [ ] Detached `delegate_task` with `d` ids in the job tools
- [ ] Result delivery through `enqueue`, deduplicated against `wait_for_job`
- [ ] Run-end wait and redirect; cancellation cleanup
- [ ] Delegate rows and `/workers` for detached children
- [ ] Live check of model behavior on Opus 5.5 and Codex
- [ ] Cache verification on a real long delegation
