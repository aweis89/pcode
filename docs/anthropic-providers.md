# Anthropic provider options

pcode reaches a Claude subscription two ways today: its own `/login`, which talks
to the Claude Code endpoint directly, and a [Meridian](https://github.com/rynfar/meridian)
proxy. This page records a September 2026 look at the risk and cost of each, the
alternatives that were considered, and what we decided. API-key access through
`ANTHROPIC_API_KEY` is unaffected by any of it.

Measurements were taken on 2026-09-23 with Claude Code 2.1.280, Meridian 1.72.0
and `claude-agent-sdk` 0.2.158.

## Recommendation

1. Treat Meridian as the default subscription route and stop defaulting to
   `/login`.
2. Smooth the rough edges of the Meridian integration, starting with the
   compaction fix. The list is under [Meridian work](#meridian-work).
3. Spike a native Agent SDK provider before committing to build one. The pass
   criteria are under [Direct SDK provider](#direct-sdk-provider).
4. Do not build ACP as a model provider. An ACP client that lets Claude Code run
   the whole conversation is a separate product decision.

| Route | Policy risk | Caching | Latency | pcode agent features | Status |
|---|---|---|---|---|---|
| `/login` direct | High | Tuned by pcode | Best | All | Shipped |
| Meridian | Low | Warm on continuation, one cold write on divergence | New CLI process per request | All | Shipped |
| Agent SDK provider | Low | Same as Meridian | One CLI process per conversation | All | Proposed |
| ACP as a provider | Low | Same as the SDK provider | Same, plus a Node process | Usage and cache data degraded | Rejected |
| ACP client | Lowest | Claude Code's own | One CLI process per session | None for Claude sessions | Separate decision |

## Policy

The risk sits in how the credential is obtained and held, not in the HTTP
transport.

Anthropic's [legal and compliance page](https://code.claude.com/docs/en/legal-and-compliance)
says third-party developers may not "offer Claude.ai login into their own
applications" and "may not collect, store, or intermediate Claude.ai credentials
or session tokens", and that sign-in "must complete through Anthropic's own
flow". pcode's `/login` runs the Claude Code OAuth client from inside pcode and
stores the resulting tokens, which is what those sentences describe.

The same page does not prevent "an end user from signing in to the unmodified
Claude Code binary with their own Claude subscription", and it describes Pro and
Max limits as assuming "ordinary, individual usage of Claude Code and the Agent
SDK". Meridian and any Agent SDK integration run the real `claude` binary with
its own login, so they sit on that side of the line.

Handing pcode's tokens to the SDK through `CLAUDE_CODE_OAUTH_TOKEN` would not
change this, because pcode would still be collecting and holding them. The CLI
also cannot refresh a token passed that way.

The wording moved during 2026. In February the page said subscription tokens
were not permitted in any other product "including the Agent SDK"
([The Register](https://www.theregister.com/2026/02/20/anthropic_clarifies_ban_third_party_claude_access/));
that sentence is gone. In June Anthropic paused a plan to bill Agent SDK usage
from a separate monthly credit, and said SDK and third-party app usage still
draw from subscription limits
([help center](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)).
If that plan returns it applies to Meridian and an SDK provider alike.

Server-side checks on direct traffic keep tightening. Commits `7dc0dff`,
`11a0d2d` and `d7a891b` exist only because the endpoint began answering
`claude_code_version_too_old`. Another harness reported plan traffic rejected
with "Third-party apps now draw from extra usage, not plan limits" when its
requests lacked the billing block that the real CLI sends as its first system
block ([hermes-agent #48176](https://github.com/NousResearch/hermes-agent/issues/48176)).
pcode does not send that block.

This is a reading of published pages, not legal advice.

## How Meridian works

Meridian is a Node proxy around `@anthropic-ai/claude-agent-sdk`, which drives
the `claude` CLI as a subprocess. In passthrough mode, which pcode uses, each
HTTP request:

1. Registers the client's tools as an in-process MCP server with stub handlers.
2. Calls `query()`, which starts a fresh CLI process that resumes the
   conversation's transcript at the last assistant message (`resumeSessionAt`
   with `forkSession`).
3. Catches each tool call in a `PreToolUse` hook, blocks it so the CLI never runs
   it, ends the turn early, and returns the captured `tool_use` blocks as an
   ordinary Anthropic response.

The next request carries the tool results and the cycle repeats. The model sees
the tools as `mcp__<server>__<name>`, and Meridian maps the names back.

### Caching through Meridian

Meridian strips client `cache_control` and lets the CLI place breakpoints.
Whether the cache stays warm depends on how it classifies each request against
the history it stored for that conversation (`verifyLineage` in the bundled
source):

| Lineage | What Meridian does | Cache |
|---|---|---|
| continuation (history only grew) | Resume the CLI session and send only the new messages | Warm |
| undo (history got shorter) | Resume truncated at an earlier message | Warm up to that point |
| compaction (only the tail still matches) | Resume the old, uncompacted session and send only the messages after the matching tail | Warm, but see below |
| diverged (edited, replayed or unrelated history) | Start a new CLI session with the history flattened into `[Assistant: …]` text | System and tools stay cached; the history is written again once |

When a request is classified as compaction, pcode's summary never reaches the
model: upstream, the CLI keeps the full history until its own auto-compaction
runs. [Meridian work](#meridian-work) has the live reproduction.

A diverged request is a one-time cost rather than a broken cache, since the
replayed session caches normally afterwards. For a 100k-token history the replay
writes those tokens at 1.25 times the input price instead of reading them at 0.1
times. This is why pcode keeps plan reminders and limit warnings append-only on
Meridian (see [dependencies](dependencies.md#anthropic-prompt-caching)).

## Measurements

The live runs used `claude-haiku-4-5`, a system prompt padded to about 9k tokens
so it clears the minimum cacheable size, and one `lookup` tool. The same
three-round tool task ran through Meridian and through the Agent SDK directly.
Meridian times run from HTTP request to complete non-streamed response; SDK times
run from delivering the tool result to the end of the next message.

Starting the CLI through the SDK (connect and initialize handshake, no inference)
took 0.60 s median over five runs. Meridian pays that on every request.

Meridian 1.72.0, passthrough, external proxy:

| Round | Time | Cache read | Cache write |
|---|---|---|---|
| 1 | 2.68 s | 0 | 11,471 |
| 2 | 1.84 s | 11,471 | 203 |
| 3 | 1.78 s | 11,674 | 142 |
| 4 (final) | 3.02 s | 11,816 | 142 |

Agent SDK with one `ClaudeSDKClient` kept alive and each tool handler parked while
the tool "runs":

| Round | Time | Cache read | Cache write |
|---|---|---|---|
| 1 | not comparable (includes connect) | 0 | 18,819 |
| 2 | 1.34 s | 18,819 | 222 |
| 3 | 1.34 s | 19,041 | 160 |
| 4 (final) | 1.04 s | 19,201 | 157 |

Two follow-ups on the same session:

- A new process resuming the session with `resume=<session id>` connected in
  0.92 s and read 19,358 tokens from cache while writing 114. Restarting costs
  nothing in cache as long as the CLI's own transcript is reused.
- A new session given the history as flattened text read 16,872 tokens (system
  and tools) and wrote 2,016 (the history).

Other observations:

- A handler parked for 75 s did not time out.
- The CLI invokes a tool handler at the end of the assistant message, about 10 ms
  before the consumer sees `message_stop`. An integration must not assume the
  handler runs afterwards.
- The same prompt cost 18,819 tokens through the SDK with `tools=[]` and
  `setting_sources=[]`, against 11,471 through Meridian, which also passes a
  `disallowedTools` list, `plugins: []` and environment variables that quiet the
  CLI. Most of the difference is cached, but it still occupies context window.
- Judging by the numbers, the CLI adds about 1.9k tokens to the first user message
  by itself (18,819 less the 16,872-token system prefix, for a 60-token prompt). A
  transcript synthesized from pcode's history would have to reproduce those bytes
  exactly to hit the cache, so synthesis is not a caching strategy.

These are small samples taken minutes apart. Treat them as direction, not a
benchmark.

## Direct SDK provider

The proposal is a Pydantic AI `Model` backed by one live `ClaudeSDKClient` per
conversation. pcode's tools are registered as in-process MCP tools whose handlers
wait until Pydantic AI runs the tool and sends the result in its next request:

```python
async def request_stream(self, messages, settings, params, run_context):
    session = self.sessions.for_conversation(run_context.conversation_id)
    if session.can_continue(messages):          # only tool results or a new prompt added
        session.deliver(tool_returns(messages))  # releases the parked handlers
    else:
        session = await self.sessions.recover(messages)
    events = to_beta_events(session.stream_until_turn_boundary())
    yield await self._process_streamed_response(events, ...)
```

Several things make this cheaper than it looks:

- `StreamEvent.event` is the raw Anthropic streaming event, and
  `AnthropicModel._process_streamed_response` accepts any async iterable of them,
  so text, thinking, tool calls and usage go through existing code. Tool names
  need mapping back from `mcp__pcode__*`.
- SDK 0.2.158 provides `tools=[]`, `setting_sources=[]`, a plain-string
  `system_prompt`, `include_partial_messages`, `set_model()`, `interrupt()`,
  `resume`, `fork_session`, `resume_session_at`, JSON Schema tool inputs and an
  injectable `Transport` for tests.
- The CLI binary recognizes `CLAUDE_CODE_MAX_RETRIES`, `DISABLE_AUTO_COMPACT`,
  `MAX_MCP_OUTPUT_TOKENS`, `MCP_TOOL_TIMEOUT` and
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, which would hand retries,
  compaction and output limits back to pcode. Their exact behaviour is
  unverified.
- No pcode credentials are involved. The child process must not inherit
  `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_BASE_URL`, or it
  silently bills the key instead of the subscription.

pcode's session store stays the source of truth for resume, `/tree`, compaction
checkpoints, recall and `cache-report`. The Claude Code transcript is a
disposable copy of what was sent, found through a saved mapping from conversation
ID to CLI session ID and CLI message UUIDs:

| Event | Action | Cache |
|---|---|---|
| Tool round or new prompt | Keep the live process | Warm |
| Restart or `--continue` | New process with `resume` | Warm (measured) |
| `/tree` to an earlier turn, or a retry | `resume_session_at` the mapped UUID | Warm up to that point |
| Compaction | New session seeded with the compacted history | One cold write of the smaller history, as on the direct path |
| Transcript missing or history edited | Flattened replay | One cold write |
| Model or tool-list change | Any | Cold on every route |

Rough work breakdown:

| Piece | Size |
|---|---|
| Model adapter and event conversion | M |
| Parked handlers, parallel tool calls, interrupt | M |
| History reconciliation and replay | L |
| Process pool keyed by conversation, cleanup, environment scrubbing | S–M |
| Settings: effort needs a respawn, model uses `set_model`, pcode cache settings off | S |
| Login UX that points at Claude Code's own login | S |
| Dependency: wheels are about 90 MB each because they bundle the CLI, or use the sdist with `cli_path` | S |
| Tests against a fake `Transport`, plus one live smoke test | M |

A spike of a day or two should pass these before the full build, which is
roughly two weeks:

- Prompt size at parity with Meridian for the same request.
- A measurable wall-clock saving per turn on a real pcode task, not a toy prompt.
- Acceptable memory per CLI process with two or three delegates running.
- Thinking, effort changes, Ctrl+C and a warm resume after restart all work.

Known costs: the model sees `mcp__pcode__*` names, built-in web search is
unavailable with `tools=[]`, transcripts land in `~/.claude/projects/` and show up
in `claude --resume`, and the SDK ships releases every few days and marks
`Transport` as internal.

Embedding Meridian's library does not avoid this work. Its entry point,
`createProxyServer()`, is the HTTP app itself, so using it still means a Node
process doing a `query()` per request.

## ACP

[claude-agent-acp](https://github.com/agentclientprotocol/claude-agent-acp)
(0.81.1 inspected) exposes the Agent SDK as an Agent Client Protocol agent. It
keeps one long-running `query()` per session, logs in through the CLI's own
`auth login --claudeai`, and lets a client override the system prompt, tools and
setting sources through `_meta`.

As a pcode model provider it would be the SDK design with more parts. pcode's
tools would have to be served as an MCP server over stdio or HTTP, and ACP
delivers text and thought chunks rather than raw Anthropic events. Per-request
boundaries, stop reasons, thinking signatures and per-request cache counts are
lost; usage arrives only as a context total.

As an ACP client, pcode would render sessions in which Claude Code runs the whole
loop with its own tools, compaction and history. That carries the lowest policy
risk and gets Claude Code's own caching, and the same client could drive other
ACP agents. It also means pcode's tools, extensions, Harness capabilities,
`/tree`, cross-provider switching and `cache-report` do not apply to those
sessions. Harness's experimental ACP module serves a Pydantic AI agent to editors,
the opposite direction, so the client side would be new code.

## Meridian work

These findings come from live runs against Meridian 1.72.0 on 2026-09-23, with
the current 1.76.1 checked by reading its bundle.

Meridian ignores pcode's compaction. The probe built a four-round tool
conversation, then sent a compacted history whose summary alone contained a
codename, and asked for it:

| Request | Lineage | Answer | Cache read | Cache write |
|---|---|---|---|---|
| Compacted, same session ID | compaction | NONE | 17,915 | 2,266 |
| Compacted, new session ID | new | PELICAN | 8,066 | 7,567 |

Under the same ID the model kept reading the full uncompacted history and never
saw the summary. Under a new ID the summary arrived, at the cost of one cold write
of the smaller history, which is what compaction costs on any route. 1.76.1
classifies compaction the same way.

Managed mode cannot start on any current release because it requires exactly
1.71.1. With that check relaxed, 1.72.0 was ready in 3.6 s using about 100 MB,
and 1.76.1 still reads `sdk-features.json`, serves `/settings/api/features` and
honours the same environment variables.

Managed mode also keeps Meridian's session store in a temporary directory, so a
resumed pcode conversation starts over. Restarting Meridian between rounds two
and three gave:

| Session store | Round 3 lineage | Cache read | Cache write |
|---|---|---|---|
| Temporary directory (today) | new | 8,066 | 1,152 |
| Persistent directory | continuation | 9,138 | 263 |

In a real session the first row's write is the whole history, replayed as
flattened text.

A stopped external proxy surfaces as a generic transient connection error that
is retried once and then reported as "Check provider/proxy connectivity". Nothing
says that Meridian is not running or not logged in. The long-running external
proxy on this machine also has Thinking Passthrough off, so thinking never
appears.

| # | Work | Size | Notes |
|---|---|---|---|
| 1 | Rotate the session ID on compaction | S | Derive `x-litellm-session-id` from the conversation ID plus a digest of the first user prompt. Compaction replaces that prompt with the summary, so the ID changes exactly then, while tool rounds, resume, model switches and `/tree` keep it. |
| 2 | Accept current releases in managed mode | S | Require a minimum version and keep the existing health and passthrough checks. Warn instead of failing above the newest verified release. |
| 3 | Preflight and clear errors | S | Check `/health` when the provider is built. Name the URL when the proxy is unreachable, point at `claude auth login` when `auth.loggedIn` is false, and use the same hint for connection failures mid-turn. Say once when thinking display is on but the proxy does not pass thinking through. |
| 4 | Persistent managed session store | S–M | Keep it under `$XDG_STATE_HOME/pcode/meridian/`. Verify that two Meridian processes can share it and that its pruning bounds disk use. |
| 5 | Choose the mode automatically | M | `meridian_managed` gains an `auto` default: the configured URL, else a proxy that answers `/health` at the default URL, else a managed instance. Start that instance in the background when the session needs it, and restart a crashed one on the next request. |
| 6 | Route `anthropic:` through Meridian | M | See below. Needs a decision first. |

Item 6 adds `meridian` as an Anthropic auth source, next to `api-key` and
`oauth`, which already share the `anthropic:` prefix. Saved sessions, effort
settings and the default model keep their names. With nothing configured, pcode
would prefer Meridian over a stored `/login` when Meridian is available, and say
so once. On this route `/login` checks `claude auth status` and points at
`claude auth login` instead of running pcode's OAuth flow. Model settings must
follow the resolved route rather than the model string: prompt-cache settings are
pointless through Meridian, and the web-search profile narrowing on the OAuth
model needs checking against what Meridian passes through. The open question is
whether pcode's own OAuth route stays at all.

Do 1 to 3 first; they are small, independent and useful straight away. Then 4
and 5, then 6 once the routing question is settled. Later candidates: a picker
fed by Meridian's account-aware `/v1/models`, an explicit command to turn on
Thinking Passthrough for an external proxy, and a way to choose the `meridian`
executable when several are installed (this machine has two).

The per-request CLI start, about 0.5 s per tool round, is inherent to Meridian;
the SDK spike covers it. When Meridian does start fresh it replays history as
flattened text. Items 1 and 4 make that rarer but cannot change the format.
