# Dependency reference guide

Use this map when changing terminal behavior or the agent runtime. Documentation
is retrieved on demand; it is not a version-matched local documentation bundle.

## Versions and sources

`pyproject.toml` defines allowed versions; `uv.lock` records resolved versions.
Check the installed environment before relying on an API. The versions below are
a snapshot, not a second set of pins: update them when dependencies change.

| Library (distribution) | Verified installed version | Official documentation | Upstream source |
| --- | --- | --- | --- |
| prompt_toolkit (`prompt-toolkit`) | 3.0.53 | [Docs](https://python-prompt-toolkit.readthedocs.io/en/stable/) | [python-prompt-toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit) |
| Rich (`rich`) | 14.3.4 | [Docs](https://rich.readthedocs.io/en/stable/) | [rich](https://github.com/Textualize/rich) |
| Pydantic AI (`pydantic-ai-slim`) | 2.43.0 | [Docs](https://ai.pydantic.dev/) | [pydantic-ai](https://github.com/pydantic/pydantic-ai) (package: `pydantic_ai_slim/`) |
| Pydantic AI Harness (`pydantic-ai-harness`) | 0.31.0 | [Docs](https://ai.pydantic.dev/harness/) | [pydantic-ai-harness](https://github.com/pydantic/pydantic-ai-harness) |

From the repository root, this read-only command prints installed versions and
package source locations without importing the agent runtime or loading credentials:

```sh
.venv/bin/python - <<'PY'
from importlib.metadata import distribution

for name, module in (
    ('prompt-toolkit', 'prompt_toolkit'),
    ('rich', 'rich'),
    ('pydantic-ai-slim', 'pydantic_ai'),
    ('pydantic-ai-harness', 'pydantic_ai_harness'),
):
    dist = distribution(name)
    print(f'{name}=={dist.version}: {dist.locate_file(module)}')
PY
```

This requires an existing project environment. Typical source locations are
`.venv/lib/pythonX.Y/site-packages/<module>/`; do not hard-code the Python minor
version. Installed packages contain implementation and docstrings, but may omit
upstream tests, examples, and documentation sources.

## Where to look for this project

- **Interactive UI:** `src/pcode/ui.py`, `src/pcode/commands.py`, and
  `src/pcode/app.py`. Start with [prompt_toolkit prompts](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/asking_for_input.html)
  and its reference sections for Application, Layout, Renderer, Input, and Output.
  Local source entry points include `prompt_toolkit/shortcuts/prompt.py`,
  `prompt_toolkit/layout/`, and `prompt_toolkit/renderer.py`.
- **Permanent terminal output:** `src/pcode/ui.py` and `src/pcode/tool_display.py`.
  Start with [Rich Console](https://rich.readthedocs.io/en/stable/console.html),
  Markdown, Syntax, and Text; inspect `rich/console.py` as needed.
  Rich owns permanent output; prompt_toolkit owns the mutable prompt and activity panels.
- **Agent creation and streaming:** `src/pcode/agent.py` and `src/pcode/live.py`.
  Start with [Pydantic AI streaming events](https://ai.pydantic.dev/agents/#streaming-all-events).
  Relevant installed source includes `pydantic_ai/agent/`, `messages.py`,
  `capabilities/`, and `models/openai_codex.py`.
- **Tools, planning, repository context, and persistence:** `src/pcode/agent.py`,
  `src/pcode/live.py`, `src/pcode/repo_context.py`, and `src/pcode/sessions.py`.
  Start with [Harness Coder](https://ai.pydantic.dev/harness/coder/), then inspect
  the installed `pydantic_ai_harness` implementation for the capabilities used.
  Verify Coder's actual tool composition, planning, and step-persistence APIs.

### Delegation activity

`src/pcode/delegation.py` bridges Harness 0.31.0's `SubAgents.event_stream_handler`
into the parent's event stream. The handler receives a **child** `RunContext`,
not the parent tool identity; `DelegationReporting.wrap_tool_execute` binds the
parent context with a `ContextVar`, reset in `finally`, so parallel children do
not share attribution. Only child tool boundaries and phase labels are forwarded,
never child text/thinking deltas. Inspect installed `subagents/_toolset.py` and
`subagents/_events.py` before changing this integration.

`live.py` consumes `DelegationStartEvent`/`DelegationEndEvent` by `tool_call_id`.
Use the structured delegation outcome: timeout/budget limits can return ordinary
successful tool strings. A max-calls refusal emits no lifecycle events; cancellation
and uncontained errors may omit the end event, so keep turn-end interruption cleanup.
Child tool IDs are scoped by parent call ID, and persisted tool events retain
`parent_call_id` for replay. The panel pins active delegates within its existing
row budget; keep `tests/test_delegation_tmux.py` exercising real CPR and resize.

### MCP integration

`src/pcode/mcp.py` uses Pydantic AI 2.43.0's `MCPToolset` and FastMCP's
`StdioTransport`; `src/pcode/live.py` supplies only enabled toolsets per run.
`MCPState.enable()` is async: for OAuth it enters/exits the prefixed MCP toolset
before publishing it as enabled. `MCPToolset.__aenter__` initializes the remote
client and completes OAuth without an agent/model run. Reuse the same toolset
object afterward so its OAuth tokens survive connection teardown. Other transports
remain lazy. The app runs activation separately from model turns, with a queue
gate and Ctrl+C/quit cleanup; keep the real-prompt tests in `tests/test_mcp_enable.py`.
Consult [Pydantic AI MCP](https://ai.pydantic.dev/mcp/client/) and
[FastMCP client transports](https://gofastmcp.com/clients/transports), then inspect
installed `pydantic_ai/mcp.py` and `fastmcp/client/transports/stdio.py`.
The `[mcp]` extra currently resolves `fastmcp-slim` 4.0.4 and `mcp` 2.2.0.
OAuth is already supported by `MCPToolset(auth="oauth")` in installed 2.43.0;
FastMCP resolves it to `fastmcp.client.auth.OAuth`. Consult
[FastMCP OAuth](https://gofastmcp.com/clients/auth/oauth) and inspect installed
`fastmcp/client/auth/oauth.py`, `fastmcp/client/transports/http.py`, and
`mcp/client/auth/oauth2.py`. In 4.0.4, default storage is **in-memory**, not disk;
the helper manages browser authorization, PKCE, callback validation, and refresh.
Do not assume older FastMCP documentation about persistent token caches applies.
Pcode intentionally uses that default rather than reading Pi credentials or adding
a credential store. The slim install omits `websockets`, but FastMCP 4.0.4's
callback server explicitly selects Uvicorn's `websockets-sansio` implementation.
Pcode adds `websockets>=15.0.1,<17` (verified with 16.1.1) so browser callbacks
actually start; mocked OAuth exchange tests alone would miss this dependency.
`src/pcode/mcp_oauth.py` supplies a narrow `OAuth` subclass to own the callback
listener. FastMCP 4.0.4 probes and closes its port at construction, then binds it
much later; a collision causes Uvicorn 0.53.0 to raise `SystemExit(3)` in a child
task. Pcode reserves an IPv4 loopback socket before registration and passes that
same socket to `Server.serve(sockets=...)`. The embedded server does not capture
process signals and converts startup `SystemExit` inside the child task into a
normal error. This avoids both the port race and interference with Ctrl+C.
If a previously registered redirect port is occupied, pcode fails normally with
instructions to disable/re-enable rather than silently changing a registered URI.
The adapter uses FastMCP's `token_storage_adapter`, the SDK's
`context.client_metadata.redirect_uris`, and Uvicorn's `capture_signals` hook;
recheck these installed-source APIs on upgrades. OAuth protocol handling, PKCE,
state validation, token exchange, and refresh remain in FastMCP/the MCP SDK.
Keep mocked-provider tests, real loopback success/cancellation tests, deliberate
port-collision tests, and the full MCP-client startup-failure subprocess test in
`tests/test_mcp_oauth.py`. Tests must not open the real browser, contact a real
service, or read real credential stores.
FastMCP defaults `StdioTransport.keep_alive` to `True`: pcode explicitly sets it
to `False` so turn cleanup closes subprocesses. Keep the real-stdio tests in
`tests/test_mcp.py` for success, failure, cancellation, and reconnection. Filtering
schemas alone is insufficient to prevent disabled servers from connecting;
disabled servers must not enter the agent's toolset collection at all.

## Verification workflow and known pitfalls

1. Check the installed version against `uv.lock`.
2. Use official docs for concepts, but verify signatures and behavior against
   installed source. Prefer release-matched docs where available; `stable`,
   `latest`, and upstream `main` are not guarantees of compatibility.
3. For upstream examples, tests, or internals, browse the matching release tag
   or commit. If deeper debugging warrants a local checkout, keep it outside
   this project's normal source tree and record its revision. Do not clone or
   install dependencies merely to read an API already available locally.
4. Validate changes with this repository's relevant regression tests (see
   `README.md`, "Validate"). Documentation alone cannot establish terminal behavior.

Specific traps already encountered here:

- Harness's latest website can describe an unreleased Coder API, extras, or a
  newer tool composition than installed 0.31.x. Follow the installed release,
  not a website example copied without verification.
- A PTY with `PROMPT_TOOLKIT_NO_CPR=1` does not exercise real prompt height:
  cursor-position reports can stretch the layout into the remaining pane. Keep
  the real-tmux height regression tests, not just PTY startup/exit checks.
- `src/pcode/repo_context.py` uses a private Harness inventory API. Recheck it
  against installed source and the repository-context tests on upgrades.
- `src/pcode/llm_proxy.py` uses Pydantic AI's private provider ownership hooks
  (`_own_http_client`, `_http_client_factory`, and Codex auth/client fields) so
  injected proxy clients close and reopen with the agent. Verified with 2.43.0
  and httpx2 2.13.0; rerun `tests/test_llm_proxy.py` on upgrades. Codex requires
  httpx2, not legacy httpx. Static models require entering the agent context;
  `run_stream_events()` alone does not manage their client lifetime.

Inspect library source and package metadata, not credential stores, `.env`
files, private keys, or token files. API discovery should not require live model
requests or authentication.

### Context indicator

`src/pcode/model_metadata.py` resolves context limits; `context_usage.py` and
`compaction.py` share its synchronous memory-only lookup. Refreshes run outside
rendering. Preserve input, output, default context, opt-in maximum, source, and
fetch timestamp separately. Unknown deployments must not inherit a familiar
model name's direct-API limit. An explicit `PCODE_CONTEXT_WINDOW` applies to both
consumers and is capped by known input/maximum limits.

- Public catalog: [Models.dev JSON](https://models.dev/api.json) and
  [schema](https://github.com/anomalyco/models.dev/blob/dev/README.md).
  Normalize only limits and provider routes, not costs. Cache atomically under
  `XDG_CACHE_HOME` for 24 hours; retain stale data on failure.
- Anthropic: [Models API](https://platform.claude.com/docs/en/api/models),
  `GET /v1/models/{model}`, fields `max_input_tokens` and `max_tokens` (nullable).
  Use the actual SDK client to preserve base URL, auth and pi credential rotation.
  Pi OAuth needs its existing beta headers and must not borrow direct-API fallback
  limits. This does not imply official support for third-party subscription use.
- Codex: [first-party models endpoint implementation](https://github.com/openai/codex/blob/f1affbac/codex-rs/codex-api/src/endpoint/models.rs),
  `GET /models?client_version=…` relative to the provider's Codex base URL.
  The live backend filters model availability by version: 0.99.0 omitted
  `gpt-6-astra`, whereas 0.154.0 returned its 272k default / 872k maximum.
  Use verified fallback 0.154.0 or a newer semantic `client_version` from
  `$CODEX_HOME/models_cache.json` (default `~/.codex/`); read only the version hint,
  never reuse cached limits/availability from a potentially different account.
  This is a versioned backend protocol, not a stable public API. Reverify on
  upgrades. Use `context_window` as the default and `max_context_window` only for
  explicit overrides. Never map `openai-codex` to `openai`.
- Verified Pydantic AI 2.43.0 (project) and 2.44.0 (installed tool) provide `Model.provider`, provider context-manager
  ownership, and `provider.client`. Codex's httpx2 auth handles credential loading,
  refresh, and one-shot 401 replay; use that same SDK client, including its proxy.
  Verified `AsyncOpenAI.get` / `AsyncAnthropic.get` accept `cast_to=dict[str, Any]`
  and per-request `timeout`, `max_retries`, headers and query parameters. Bare
  `cast_to=dict` fails in installed Anthropic 1.6.0. Do not clone pi's custom SDK
  client with `with_options`, which requires constructor arguments it cannot infer.

Native metadata is kept in memory per exact model instance, not in a shared
account cache. Three-second fetch deadlines and one-minute failure backoff prevent
metadata from breaking chat; cancellation must still propagate. Adapter tests use
synthetic credentials/MockTransport, not live authenticated requests. The Astra version-filter diagnosis was
  additionally verified with read-only live catalog requests (no model inference).
Pydantic AI `RequestUsage.input_tokens` includes cache read/write tokens already;
show only the latest response's input usage, never cumulative billing usage.


### Context compaction

`src/pcode/compaction.py` uses installed Harness 0.31.0's `SummarizingCompaction`
and `compact_now(strategy, messages, model=..., focus=..., usage=...)`. Consult
[Harness compaction](https://pydantic.dev/docs/ai/harness/compaction/) and inspect
`pydantic_ai_harness/compaction/_manual.py`, `_summarizing_compaction.py`, and
`_shared.py` in the installed environment. Pcode isolates private token/cutoff helpers
and `drain_summary_events` here; verify these on upgrades. Streaming the dedicated,
tool-free summary request is required for streaming-only subscription endpoints.
Use the actual resolved model object to retain custom auth/proxy behavior.

Installed Coder includes `ClearToolResults(max_fraction=0.7)`; pcode removes it to
avoid discarding evidence before summarization. The installed summarizer defaults
to 500 characters per tool result, which pcode explicitly raises to 16,000. Its
`keep_tokens` cutoff can retain an oversized final tool batch, so pcode detects that
case and summarizes the whole settled history with `keep_messages=0` instead.

Manual checkpoints serialize `ModelMessage`s into a private, fsynced journal event
that creates/selects one immutable conversation-tree node. Automatic compaction
uses the public `ContinuableSnapshot` / `StepStore.save_snapshot` API before the
next model request; subsequent Harness `StepPersistence` snapshots supersede it.
Never use synthetic agent runs to install summaries or overwrite original snapshots.
Keep tests for immediate restart, branch isolation, failure/cancellation, safe tool
pairs, mid-tool-loop compaction, and stale usage anchors after rewriting history.
