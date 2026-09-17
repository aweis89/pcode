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

### MCP integration

`src/pcode/mcp.py` uses Pydantic AI 2.43.0's `MCPToolset` and FastMCP's
`StdioTransport`; `src/pcode/live.py` supplies only enabled toolsets per run.
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
Keep both mocked-provider tests and real loopback callback success/cancellation
tests in `tests/test_mcp_oauth.py`. Tests must not open the real browser, contact
a real service, or read real credential stores.
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
