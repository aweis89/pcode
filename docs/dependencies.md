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
| Pydantic AI (`pydantic-ai-slim`) | 2.45.0 | [Docs](https://ai.pydantic.dev/) | [pydantic-ai](https://github.com/pydantic/pydantic-ai) (package: `pydantic_ai_slim/`) |
| Pydantic AI Harness (`pydantic-ai-harness`) | 0.31.1.dev48+12bce878 (commit `12bce878da99bca61a5d8d798bff0a3bc93bd153`) | [Docs](https://ai.pydantic.dev/harness/) | [pydantic-ai-harness](https://github.com/pydantic/pydantic-ai-harness) |
| Playwright (`playwright`, via the Harness `playwright` extra; Chromium downloaded on first `/browser` use) | 1.63.0 | [Docs](https://playwright.dev/python/) | [playwright-python](https://github.com/microsoft/playwright-python) |

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

### Local Harness checkout

Harness upstream source is the one dependency worth reading in full, because the
installed wheel omits the `docs/`, `tests/`, `examples/`, and `integration_tests/`
trees. `make harness-src` checks it out under `tmp/pydantic-ai-harness`
(gitignored) at the SHA pinned in `pyproject.toml`:

```sh
make harness-src   # prints "tmp/pydantic-ai-harness @ <sha>"
```

The target reads the SHA from `pyproject.toml`, so it cannot drift from the pin.
It clones when the directory is absent, fetches only when the pinned commit is
missing, and leaves a **detached** HEAD at that commit — rerun it after changing
the pin, and never commit work on top of it. Takes about six seconds cold.

`docs/coder.md`, `docs/repo-context.md`, `docs/compaction.md`,
`docs/subagents.md`, and `docs/shell.md` cover the capabilities pcode composes.
Read these instead of the website, which tracks `main` and can describe an
unreleased API. Installed `site-packages` still decides what actually runs: the
checkout is for docs, tests, and history, not a substitute for verifying the
installed source. Do not build or install pcode from it — the dependency is
pinned by SHA.

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

### File access and explorer shell

`src/pcode/workspace_filesystem.py` retains pcode's path/protection policy around
Harness's `FileSystem` and `FileSystemToolset`: relative paths keep the workspace base, but absolute paths,
parent traversal, and external symlinks are allowed. Removing only the containment
check is insufficient: list/search/find inline `relative_to` calls, events need a
reconstructable location, and missing-parent writes assume a workspace-relative
parent. The adapter retains upstream read/write/edit implementations and adapts
the three walkers. `DisplayFileSystem` layers mutation evidence on top of it;
compare those with installed source on upgrades. Keep
`tests/test_filesystem.py`, the real-tool tests in `tests/test_live.py`, and the
repository-context tests when changing this integration.

Allow/deny matching remains workspace-relative inside the workspace and absolute
outside it. Legacy walker results keep that convention; the selected Coder
`list_files`/`grep` tools return paths relative to `cwd`, including external `..`
paths. Protected write patterns still apply at any depth. File events retain
relative `path` plus absolute `root_dir`.

Forward `cwd`, `tools`, `content_hashes`, and `max_read_chars` when constructing
the display toolset. Omitting them silently restores legacy tools and hash-bearing
schemas, and removes Coder's read pagination budget. The `coder` extra supplies
ripgrep, but an installed `pcode` entry point does not activate its environment's
`bin` on PATH. `create_coder` appends that bin directory when `rg` is absent,
without changing existing executable precedence. Test outside `uv run` too.

Explorer receives the parent's file selection as read-only and a distinct stock
`Shell` with the same settings and environment filtering. The no-edit rule is
behavioral guidance, not confinement. Preserve `tests/test_explorer_shell.py`
and `tests/test_coder_integration.py`. Persistent shell refers to process lifetime,
not sticky `cd`: each call still starts at the workspace.

`AutomaticRepoContext` bridges `FilesSearchedEvent` into upstream's traversal
handler because `list_files` and `grep` no longer emit `DirectoryListedEvent`.
Keep the workspace containment and per-run deduplication checks; searches outside
the workspace must not inject external instruction files.

### Repository instruction discovery

[Harness RepoContext](https://pydantic.dev/docs/ai/harness/repo-context/) supports
ancestor loading in installed 0.31.0, but `home_dir=None` scans only the workspace.
Verified in `repo_context/_loader.py`: the bound is inclusive; when it is not an
ancestor of the resolved workspace, the loader falls back to workspace-only.
`src/pcode/repo_context.py:create_repo_context` therefore supplies the resolved home
for workspaces beneath it, or the filesystem root elsewhere, for both the main
agent and explorer. Harness handles ancestor-first ordering, both instruction
filenames, real-path/content deduplication, and per-run cache isolation. No custom
instruction scanner is needed. `repo_context_walk_up=off` instead supplies no
bound, retaining workspace-local instructions. `repo_context_nested` independently
selects `off` (default), `pointer`, or `contents` through Harness's
`nested_traversal`/`nested_inject` options. Both preferences are snapshotted at
agent creation, not reloaded mid-run; the asset inventory remains workspace-local.

Installed 0.31.0 detects traversal via filesystem `FileReadEvent` and
`DirectoryListedEvent` capability events, not the deprecated tool-name sniffing
options shown in some website examples. Notes are enqueued in the conversation
tail, leaving the startup prefix stable. `_loader.find_dir_context_file` selects
only the first filename for nested discovery (unlike the startup walk, which
loads both). The traversal hook checks only the accessed directory within the
workspace, not intervening parents; it can also surface workspace instructions
already loaded at startup. Its per-run seen-directory set is separate from the
startup loader's deduplication. Keep `tests/test_repo_context.py` covering the
boundary, symlink, deduplication, refresh, and all discovery-setting combinations
with real filesystem tools on both main and explorer agents when upgrading.

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

Do not add `DelegationEndEvent.usage` to session totals. `SubAgent.usage_limits`
isolates a child's *request count* (that is what bounds a runaway child and turns
exhaustion into a steering message rather than a raised limit), but its tokens
still arrive in the parent's `result.usage`, so adding them again doubles every
delegated token. Verified against the installed `_toolset.py` and a delegated run.
Sub-agents also receive only `shared_capabilities`, never the per-run capabilities
the parent passes to `run_stream_events`: cache settings and step persistence must
be installed there, and `SavedSession.recover()` must skip runs with a
`parent_run_id`. See [prompt caching and plan reminders](prompt-caching.md).
Child tool IDs are scoped by parent call ID, and persisted tool events retain
`parent_call_id` for replay. The panel pins active delegates within its existing
row budget; keep `tests/test_delegation_tmux.py` exercising real CPR and resize.

### MCP integration

`src/pcode/mcp.py` uses Pydantic AI 2.43.0's `MCPToolset` and FastMCP's
`StdioTransport`; `src/pcode/live.py` supplies only enabled toolsets per run.
Unless a server sets `"direct": true`, `build_toolset` marks its tools
`defer_loading=True` (via `defer_loading()`, inside the prefix) so Pydantic AI's
auto-injected `ToolSearch` reveals them through `search_tools`. `FunctionModel`
claims **every** native tool by default, including server-side tool search it
cannot run, so a test that expects the local `search_tools` fallback must pass
`profile=ModelProfile(supported_native_tools=frozenset())`; otherwise the tools
are withheld with no discovery surface at all. Use `mcp.mcp_transport()` rather
than walking `.wrapped` by hand, since the wrapper chain now varies per server.
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
Pcode intentionally uses that default rather than adding a credential store. The slim install omits `websockets`, but FastMCP 4.0.4's
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

### Codex sign-in

`src/pcode/codex_login.py` uses the installed Pydantic AI public
`OpenAICodexOAuthFlow` and `OpenAICodexCredentialSource` APIs. Pydantic owns PKCE,
token exchange, the localhost:1455 callback, and refresh; pcode owns browser
opening and an atomic owner-only credential store. No Codex CLI or Python Codex
SDK is required for this flow. Provider `load()` / `save()` callbacks persist
rotated tokens. The CLI credential loader remains the fallback when pcode's
file is absent, including when `PCODE_LLM_PROXY` is configured. The stores stay
separate and pcode never writes or deletes the CLI's file.

### Anthropic subscription sign-in

`src/pcode/anthropic_oauth.py` owns pcode's own `/login`: PKCE (S256)
authorization code, loopback callback, token exchange/refresh, an owner-only
credential file, and the `AnthropicOAuthModel` transport. `src/pcode/auth.py`
holds the Claude Code wire markers (`SubscriptionOAuthWire`,
`_subscription_oauth`).

The flow parameters are not published API. They were verified against
independent implementations rather than copied from a blog post, including
[modelbridge's `oauth/claude.rs`](https://docs.rs/modelbridge/latest/src/modelbridge/oauth/claude.rs.html).

They use the public Claude Code `client_id`, `https://claude.ai/oauth/authorize`
with `code=true` and `code_challenge_method=S256`, and echo the PKCE verifier
back as `state` (the callback and the token exchange both compare against it).
Their differing loopback ports (53692 vs 54545) are the evidence that the
redirect port is not fixed; pcode defaults to 54545 with
`PCODE_OAUTH_CALLBACK_PORT` as the override. Use the current token endpoint,
`https://platform.claude.com/v1/oauth/token`: `console.anthropic.com` is the
older host still present in third-party code. Re-verify on upgrades;
entitlements and server behavior can change without notice.

Installed Anthropic SDK 1.6.0 provides a **public** async credentials hook, so no
private client override is needed: `AsyncAnthropic(credentials=provider)` where
`provider` is `async def (*, force_refresh: bool = False) -> AccessToken`
(`anthropic/lib/credentials/`). Its `TokenCache` caches in memory, refreshes
proactively with single-flight semantics, and `_should_retry` invalidates plus
replays once on a 401 with `force_refresh=True`; `AccessTokenAuth` sets
`Authorization: Bearer` and the `oauth-2025-04-20` beta per request. Passing
`credentials=` also suppresses credential environment lookups, but `base_url`
must still be passed explicitly so `ANTHROPIC_BASE_URL` cannot redirect
subscription traffic. Recheck this hook on SDK upgrades; if it disappears,
`custom_auth` plus `_validate_headers` is the private fallback. Blocking file and
token-endpoint work runs in `asyncio.to_thread`, and cross-process refreshes are
serialized with `filelock` (already used by preferences).

`tests/test_anthropic_oauth.py` must keep: the authorization-request assertions,
real loopback callback success, state-mismatch/error/wrong-path rejection,
timeout and busy-port errors, stored-file validation, 0600 permissions, refresh
with and without a rotated refresh token, single-flight refresh, failure messages
that never contain bodies or tokens, and the end-to-end 401 → forced refresh →
retry path through the real SDK. Tests must never open a browser, reach a real
endpoint, or read the developer's credential file; `tests/conftest.py` redirects
`XDG_CONFIG_HOME` and clears `PCODE_CREDENTIALS_FILE`.

## Verification workflow and known pitfalls

1. Check the installed version against `uv.lock`.
2. Use official docs for concepts, but verify signatures and behavior against
   installed source. Prefer release-matched docs where available; `stable`,
   `latest`, and upstream `main` are not guarantees of compatibility.
3. For upstream examples, tests, or internals, browse the matching release tag
   or commit. For Harness, run `make harness-src` (see "Local Harness
   checkout"); for other dependencies, keep any checkout outside this project's
   normal source tree and record its revision. Do not clone or install
   dependencies merely to read an API already available locally.
4. Validate changes with this repository's relevant regression tests (see
   [development.md](development.md#validate)). Documentation alone cannot establish terminal behavior.

Specific traps already encountered here:

- Harness's latest website can describe an unreleased Coder API, extras, or a
  newer tool composition than the pinned snapshot. Follow the installed source,
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
  Use the actual SDK client to preserve base URL, auth and credential rotation.
  Subscription OAuth needs its existing beta headers and must not borrow
  direct-API fallback limits. This does not imply official support for third-party subscription use.
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
  `cast_to=dict` fails in installed Anthropic 1.6.0. Do not clone a custom SDK
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

### Tool output limits

`tool_output_limits.py` composes the pinned Harness `ToolOutputLimits`, `Band`,
`Spill`, `Truncate`, `LocalFileStore`, and `indented_json` APIs. Coder already
includes a private `ToolOutputLimits` subclass with a 64k truncation band: replace
it by type rather than append another limiter, or data can be lost before spilling.
`SubAgents.shared_capabilities` receives a separate instance with the same settings
and store. Keep retrieval registered even with no bands, since resumed histories
can contain older handles. Spills are separate from the session journal and survive
`--no-save`; tests must isolate `XDG_STATE_HOME` as well as config/cache directories.

The pinned `read_tool_result` pages by lines, then caps the body at 50k characters.
A single longer line cannot be recovered with `offset` or `from_end`; the coding
adapter supplies the stable store path in instructions for shell-based character
slicing. `indented_json` makes structured returns pageable but does not split long
string fields. Verify this behavior against the installed source on upgrades.

`CodingToolOutputLimits` reduces only the persistent shell's body, leaving its
PID/log/status footer intact even for head truncation and small budgets. It marks
changed bodies so `shell.result_projection` omits previews whose clipping removed
redaction context. Length-based detection alone stops working after reduction,
especially for delegated calls that have no `CommandFinishedEvent` in the parent.
Keep the real-shell parent/explorer tests, long-line recovery, serialized-history
readback, and spill-failure fallback tests in `tests/test_tool_output_limits.py`.

### Reasoning effort

`preferences.apply_effort` uses `openai_reasoning_effort` for OpenAI/Codex and
`anthropic_effort` for Anthropic/Meridian.
Verified Pydantic AI 2.43.0's `AnthropicModelSettings.anthropic_effort` and
`AnthropicModel._build_output_config` send `output_config.effort` without changing
thinking settings. The model profile's `anthropic_supports_xhigh_effort` selects
native `xhigh`; otherwise pcode maps its top level to `max`. Support for effort
and its highest levels varies by model; do not infer support from the route alone.
See [Anthropic effort](https://platform.claude.com/docs/en/build-with-claude/effort).

### Anthropic prompt caching

See [prompt caching and plan reminders](prompt-caching.md) for the upstream design
history, why the durable-user-prompt anchor was insufficient for long tool loops,
and the tradeoffs of pcode's append-on-change fix. `make cache-report` reads the
provider's own per-request verdict out of saved sessions; run it after changing
anything that touches message history.

Verified against Pydantic AI 2.45.0 and saved-session usage records: the Anthropic
adapter adds no `cache_control` of its own. Without explicit settings an Anthropic
conversation re-reads its whole prefix at full price on every request. `agent.model_settings`
therefore sets `anthropic_cache` (the server-side automatic breakpoint, which moves
forward as history grows) plus `anthropic_cache_instructions` and
`anthropic_cache_tool_definitions`. Capturing real request bodies is the only proof
that breakpoints reach the wire; `tests/test_prompt_cache.py` asserts top-level
`cache_control`, the last tool, and the last system block.

Harness's upstream `Planning` appends an ephemeral reminder and anchors its
`CachePoint` on the last durable user prompt, not the latest tool result. The next
request removes the previous reminder, invalidating the automatic cache entry
covering it. Saved usage showed reads stuck at the old user-prompt boundary while
the growing tool-loop tail was repeatedly written. Checking only that cache
controls reach the first request does not catch this; test a multi-request loop.

Meridian is excluded deliberately. Installed 1.71.1's proxy strips client
`cache_control` (`stripCacheControlDeep`, `stripCacheControlForHashing`) and decides
reuse from its own lineage hash over the full semantic message prefix; only
`thinking` / `redacted_thinking` blocks are exempt (`HASH_IGNORED_BLOCK_TYPES`).
Sending cache settings there changes nothing, and a mutable tail that moves each
request diverges the lineage regardless.

For every provider, pcode's `IdentifiedPlanning` appends durable plan snapshots only
when the rendered plan changes (including clearing it), without moving explicit
cache markers. `MeridianLimitWarnings` retains old warnings and appends updates at
percentage deciles or severity changes only on Meridian; other providers' limit
warnings are unchanged. Both use `before_model_request`, whose messages Pydantic AI
persists, not the ephemeral `wrap_model_request` boundary. Deduplication compares
the text of the last reminder in the current history, so saved resume, retry, and
branch selection do not depend on process-local state. Do not store the dedup key
in `ModelRequest.metadata`: Pydantic AI's history normalization merges consecutive
requests and keeps only its reserved `__pydantic_ai__` namespace, so an
application marker silently vanishes on resume and every turn re-appends its
reminder. Anthropic and native Codex
wire-prefix regressions live in `tests/test_meridian_reminders.py` and
`tests/test_planning_cache.py`.

Check the running proxy's `/health` version rather than trusting `meridian --version`:
the launch service can use a different Node installation than the shell. The
append-only requirement was reproduced against Meridian 1.72.0's lineage checker.

### Cache-bust warnings

`cache_warnings.py` subclasses the pinned Harness `WarnOnCacheBusts` and converts
its `CacheBustWarning` into a capability event. Import from
`pydantic_ai_harness.warn_on_cache_busts`, not the root used by the docs' first
example (the pinned package does not re-export it). The inherited `for_run`
uses `dataclasses.replace`, preserving the adapter while resetting detector state.

The pinned `after_model_request` coroutine never suspends. Its narrow
`warnings.catch_warnings(record=True)` scope ends before `ctx.emit`, which can
suspend; do not expand capture across model/tool execution. Recheck this on
upgrades, especially for Python versions where warning filters are process-global.
Explicit ignore/error filters still apply, and unrelated warnings are re-emitted.
Only the warning's first paragraph is shown, excluding its Python suppression
example. Detection, per-model keys, TTL hint, and collapse latch stay upstream.

The main Coder and `SubAgents.shared_capabilities` install the adapter. Child
warnings are forwarded by `stream_child_activity` without forwarding child prose.
`CacheBust` presentation events are journaled and replayed as literal warnings,
not inserted into model history. Tests in `tests/test_cache_warnings.py` exercise
real streamed cache usage, isolated runs, delegation, warning filters, terminal
handoff ordering, and saved-session/redraw replay. Monitoring is per agent run,
not conversation-wide; no cache history is restored from saved sessions.

### Cache-collapse fingerprints

`cache_diagnostics.py` answers the question the upstream warning cannot: the
provider reports only token counts, which conflate *the prefix was rewritten* with
*the cache expired*. Both produce an identical collapse, and upstream deliberately
does not attribute a cause. Every request is therefore fingerprinted into a bounded
per-run window, and `divergence()` compares the collapsing request with the healthy
one before it. `Prefix intact` (all earlier messages byte-identical, the rest
appended) rules out mutation and points at TTL or breakpoint placement; otherwise
the first moved message index is named. Instructions, tool definitions and cache
settings are checked first because they sit ahead of every message, so a change
there would otherwise be misattributed to the first message that follows it.

Fingerprints are digests, sizes, part kinds and breakpoint positions — never prompt
text. Prompts carry file contents and command output, so a debug dump of them would
leak precisely what `diagnostics.redact` exists to prevent. The dump is written only
when a collapse fires, so the healthy path costs one hash per request.

Two failure modes are load-bearing and covered by tests: the step counter is
independent of the bounded window (the window's length stops growing, which would
repeat step numbers for exactly the long runs a collapse shows up in), and dump
filenames de-duplicate (two runs in one process can collapse in the same second at
the same step). A fingerprinting failure clears the window and degrades to the
plain upstream warning rather than ending the run, since part shapes vary by
provider and capability. `PCODE_CACHE_DIAGNOSTICS` disables (`off`) or redirects
the dumps; tests must isolate `XDG_STATE_HOME`.

### Meridian conversation identity

Verified against installed Meridian 1.71.1, Pydantic AI 2.44.0, and Harness 0.31.0:
Meridian's `passthrough` adapter reads `x-litellm-session-id` (not
`x-session-affinity`). Without it, client-owned tool-result rounds take the
`independent-request:headerless-tool-result` path and start fresh SDK sessions,
replaying history instead of resuming native turns.

`MeridianSessionIdentity.before_model_request` copies request settings/headers and
supplies Pydantic's `RunContext.conversation_id`. The runtime already preserves
that ID on saved resume and model changes, and rotates it on `/new`. Harness's
`SubAgents.shared_capabilities` applies the same policy to every child, including
disk-defined agents: fresh child runs have their own Pydantic conversation IDs.
Do not store identity in the provider's default headers: children share models
and HTTP clients, including during parallel delegation. Non-Meridian requests
must remain untouched. `tests/test_meridian.py` exercises HTTP serialization,
tool loops, resume from history, and parallel child identity separation.

Meridian 1.71.1's passthrough transform does not advertise `supportsThinking`;
its stream path strips thinking blocks unless `thinkingPassthrough` is enabled.
The default is false. Inspect the running proxy's effective settings with a
read-only GET of `/settings/api/features` (the `passthrough` entry), or use its
`/settings` UI. Do not silently change global proxy settings from a display toggle.
`tests/test_meridian.py` covers both forwarded and absent thinking blocks through
Anthropic SSE decoding and `AgentRuntime`'s saved `ThinkingDelta`/`Thinking` events.
Only readable provider text enters these events; native model-message history
remains separate and may also contain opaque signatures.

### Codex thinking streaming

Verified Pydantic AI 2.43.0's `OpenAICodexModel` inherits the Responses model's
`openai_reasoning_summary` setting. `_build_reasoning` serializes `"detailed"` as
`reasoning.summary`; without it, reasoning effort alone does not request visible
summary text. Pcode requests summaries for Codex independently of `show_thinking`
so the scrollback view can be enabled mid-turn. This requests provider-exposed
summaries, not raw internal reasoning, and does not change effort. Other routes
are unchanged. See the setting's installed-source documentation in
`pydantic_ai/models/openai.py` and
[OpenAI reasoning summaries](https://platform.openai.com/docs/guides/reasoning#reasoning-summaries).
`tests/test_codex_profile.py` checks the serialized request and preservation across
effort changes; event persistence and terminal visibility are covered separately.

### Anthropic thinking requests

For direct `anthropic:` routes (API-key and OAuth authentication), `show_thinking=on`
now also opts into thinking generation on the next turn. `preferences.apply_thinking`
uses the installed model profile's `anthropic_supports_adaptive_thinking` flag:
adaptive models receive `anthropic_thinking={"type": "adaptive", "display": "summarized"}`;
older models receive `{"type": "enabled", "budget_tokens": 2048, "display": "summarized"}`,
with the budget below the adapter's default
4096 output-token limit. This requires a thinking-capable model. Adaptive models
can choose not to think on a particular response. Enabling thinking can increase
latency and token usage; this does not modify the separately selected effort.

Verified against Pydantic AI 2.43.0's `AnthropicModel.prepare_request`,
`_translate_thinking`, and `_messages_create`. The profile check is important:
newer models reject budgeted thinking. Before login, use `anthropic_model_profile`
without forcing credential resolution. Settings dictionaries are replaced, not
mutated, so in-flight requests are unaffected; off removes the opt-in and restores
the provider default. Startup, model switching, session resume, `/show-thinking`,
and Ctrl+T apply the same policy. Meridian remains display-only and still requires
proxy-side Thinking Passthrough; never change its global settings automatically.

`tests/test_anthropic_thinking.py` checks serialized adaptive/budgeted requests,
real Anthropic SSE decoding into readable thinking events with both API-key and
OAuth transports, off/on/off transitions, preserved effort, deferred login, switching,
and resume. Installed Anthropic SDK 1.6.0 accepts `display`, and Pydantic AI
2.43.0's `_translate_thinking` passes that dictionary through unchanged. Explicit
`summarized` is important for models whose API default omits readable thinking. See
[Anthropic extended thinking](https://platform.claude.com/docs/en/build-with-claude/extended-thinking)
and [adaptive thinking](https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking).

### Saved thinking and scrollback replay

`live.py` maps readable `ThinkingPart`/`ThinkingPartDelta` content into
`ThinkingDelta` and emits a `Thinking` completion at `PartEndEvent`. These events
are journaled independently of visibility. Never put signatures or redacted
thinking data in them. `SavedSession.transcript_records` coalesces thinking deltas,
flushes them before interleaved display records, skips duplicate completion text,
and preserves incomplete blocks on cancellation/failure/reopen. Resume captures
these writes into `TranscriptLog` under the same `transcript_max_chars` budget as
live output, then requests the ordinary redraw; it has no separate record limit.
Native model history remains separate from this readable presentation history.

`TerminalOutput.thinking_delta` commits complete plain-text lines through
`Transcript.thinking`, flushing the unfinished line on block/turn termination.
Thinking is control-sanitized and display-redacted like command text, and uses the
named Rich `pcode.thinking` style (muted + dim; terminal-color mode uses dim default).
Define compound styles in the theme: a string such as `pcode.muted dim` is not a
valid composite of a theme alias and an attribute in Rich's style parser.
Consecutive thinking writes coalesce in `TranscriptLog`, without the old 8-KB
preview truncation. Retention is bounded by the text a replay would emit, not by
entry count. An entry-only cap previously dropped visible history from long
sessions on every resize. Live Markdown blocks and user prompts are now recorded
atomically with their separators, so a trailing blank cannot evict an oversized
newest message. The loose entry cap scales with the character budget and remains
only so a flood of tiny writes cannot grow the deque forever.
Markdown is retained as its source (`RetainedMarkdown`), because `Transcript.print`
rebuilds the renderable with the current theme anyway and a parsed token tree
costs tens of times its source.

`show_thinking` only controls the projection of these stored writes (plus the
Anthropic next-request opt-in above). Ctrl+T and `/show-thinking` request the same
atomic rebuild as `/redraw`. Hidden writes must remain in the log; pending writes
must not duplicate on regeneration or resize. No thinking is allocated to the
live prompt or task header. Legacy `thinking_display`/`thinking_lines` preferences
are ignored. Keep real-tmux tests for streamed text before completion, show/hide
while editing, cancellation/completion retention, history deduplication, resize,
and compact prompt/task height with real CPR. A PTY without CPR is insufficient.

Persistence is intentional even when hidden. Do not promise secure deletion by
toggling visibility or clearing terminal history, and do not backfill old sessions
from potentially opaque native model history without a separate migration design.

### Explicit newline key encodings

`input_keys.py` registers narrow VT100 aliases before constructing a prompt:
CSI-u and xterm modifyOtherKeys Ctrl+J / Shift+Enter become `Keys.ControlJ`.
Verified against prompt_toolkit 3.0.53: CSI-u is not decoded by default, and
`ESC [ 27 ; 2 ; 13 ~` otherwise maps to `ControlM` (submit). Registration uses
its process-global `ANSI_SEQUENCES` table and clears the private
`_IS_PREFIX_OF_LONGER_MATCH_CACHE`; recheck these internals on upgrades.
No keyboard protocol is enabled and this is not general Kitty support.

Inspiration: [pi-vim](https://github.com/lajarre/pi-vim/blob/main/index.ts)
passes insert-mode input to Pi's editor and implements `o`/`O` with explicit
newline insertion; [Pi's key decoder](https://github.com/earendil-works/pi/blob/main/packages/tui/src/keys.ts)
distinguishes legacy, CSI-u, and modifyOtherKeys input. We retain prompt_toolkit's
native vi `o`/`O` bindings. Pipe-input tests cover both editing modes and prompt
implementations; real-tmux tests retain CPR, multiline height, Escape, and resize
checks for each supported newline encoding.


### Provider summaries versus internal reasoning

[OpenAI's reasoning guide](https://developers.openai.com/api/docs/guides/reasoning)
describes exposed summaries, not raw internal tokens. `summary="auto"` does not
mean heading-only or promise a particular update frequency. Installed Pydantic AI
2.43.0 maps decoded summary content to `ThinkingPart`/`ThinkingPartDelta`.
[Anthropic's thinking guide](https://platform.claude.com/docs/en/build-with-claude/thinking)
also describes visible summaries and empty blocks with `display="omitted"`.
Neither reasoning-token usage nor a thinking-status event proves readable text
exists. We stream all exposed text rather than extracting a task-header heading;
we cannot reconstruct provider-omitted content.


## Live shell output

Harness is pinned by full Git SHA in `pyproject.toml`, not only a uv source
override, so `make install` also receives the pin. Hatch requires
`allow-direct-references = true` for editable and wheel builds with this dependency.
The verified revision is `12bce878da99bca61a5d8d798bff0a3bc93bd153`.

Coder now selects `Shell(tools=['shell'], default_timeout=270)`. Pcode uses that
class unchanged; `src/pcode/shell.py` only projects its `CommandStartedEvent`,
`CommandOutputEvent`, and `CommandFinishedEvent` into transient UI output. Events
carry `tool_call_id`; keep buffers local to a run and keyed by that identity.
They do not add model calls. Planning and SubAgents are no longer in Coder and
must be composed explicitly. `ClearToolResults` remains removed so pcode can
summarize before discarding evidence.

The persistent executor polls the combined stdout/stderr log every 50 ms, emitting
at most the first 16,000 bytes in chunks up to 4,096 bytes. Its final result is the
last 16,000 bytes plus PID/log/status handles. `CommandFinishedEvent` means the
foreground wait ended, not necessarily that the process exited: `exit_code=None`
means running. The final result can carry a later terminal status than the event;
do not overwrite that exit with an earlier running snapshot. The event's PID is
the supervisor/session leader; the status JSON's PID is its child command, so
comparing those PIDs to validate a status discards genuine completions. Foreground timeout
returns handles without killing the process. Cancellation while waiting kills the
session; processes whose handles were already returned outlive the run.

An already-truncated result can begin inside a secret whose opening marker was
dropped. `result_projection` conservatively omits that raw tail from UI/inspection
payloads, keeping supervisor handles/status; it does not reread the log. Apply it
to delegated shell results too. The model result and raw upstream log are not
redacted by this display adapter. `--no-save` does not disable upstream logs.

Wait for complete lines, sanitize before clipping, and redact unfinished quoted
credentials and private-key blocks before displaying a preview. `CommandOutput`
bypasses session/tree journals. Remove per-call previews when tool results arrive,
and clear them on cancellation/failure/reset. Preserve `tests/test_shell_streaming.py`
and the real-tmux command-height tests; no-CPR PTYs cannot prove compact height.

### Managed Meridian isolation (verified installed 1.71.1)

Official reference: <https://github.com/rynfar/meridian/blob/main/docs/configuration.md>.
Resolve `meridian` through the version-manager shim before inspecting its package.
The installed `@rynfar/meridian/dist/cli-ryt69ryf.js` implements
`MERIDIAN_CONFIG_DIR` / `sdk-features.json` (adapter-keyed objects),
`MERIDIAN_SESSION_DIR`, `/health`, and `/settings/api/features`. A healthy response
contains `status: healthy` and `version`; an unauthenticated response contains
`auth.loggedIn: false`. The effective `passthrough.thinkingPassthrough` must be true.
The managed launcher pins this contract to 1.71.1 rather than assuming newer
website documentation matches the installed package.

Config-directory isolation alone is insufficient: disk profiles and default
telemetry/plugin/update paths can still resolve under the real home. The managed
launcher explicitly isolates plugins and design-token state and disables persisted
telemetry/update checks. Existing disk profiles and Claude authentication remain
shared intentionally; do not describe this mode as a credential sandbox. A local
smoke check started a private instance, verified health and effective settings,
and terminated it without making any model request.


### Completed file diffs (verified pinned Harness revision)

`FileWrittenEvent` still has no before/after contents. The new `FileEditedEvent`
contains a bounded diff, but it is already truncated and not secret-redacted;
it cannot replace pcode's full-source redaction and precise counts. Reading the
file when either event reaches the UI cannot recover the previous version.
`DisplayFileSystemToolset` keeps in-operation evidence capture. On upgrades,
compare `_write_file` and `_edit_file` with installed source, including descriptor
checks, hashes, recoverable errors, canonical newlines, and result strings.

`_edit_file` now takes a sequence of `Replacement` objects, not old/new positional
strings. Apply all replacements before one guarded write, honor the upstream
change-request cancellation/revalidation, and emit one completed diff. Forward
`content_hashes=False` into the toolset and use `_hash_suffix` for model results;
events still carry hashes. The write adapter retains pcode's existing descriptor
snapshot semantics rather than upstream's pre-write request/snapshot flow.

Writes capture old contents through the same descriptor before truncation.
Unconditional writes try read/write access for capture, falling back to the
original write-only access when reading isn't permitted; missing evidence must
not make an otherwise valid write fail. The edit adapter uses the exact text
already read for replacement. Neither adapter makes the underlying operation
transactional against arbitrary external writers.

Pydantic AI exposes tool arguments through `ToolCallPartDelta.args_delta`.
`StreamingEditPreview` handles single pairs and replacement arrays without
executing anything, using
`pydantic_core.from_json(..., allow_partial="trailing-strings")` for incomplete
string values. A separate parse with `allow_partial=True` requires a complete
path before exposing content; resolve it against the filesystem root to exclude
sensitive symlink targets too. Preview events bypass the session journal;
completed change events replay through the same recorded transcript method even
when hidden. Keep `tests/test_edits_tmux.py`: no-CPR PTYs don't verify the shared
preview/editor height budget or redraw behavior in a real terminal.
