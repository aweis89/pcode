# Tools

## Tool permissions

**Live mode enables actual Coder file edits and shell tools. There is no approval
UI.** pcode does not implement its own permission model and does not use prompt
text as a safety control. Permission management is out of scope: run pcode inside
a sandboxing wrapper (a container, VM, or an OS sandbox such as `sandbox-exec`
or `bwrap`) when you need enforcement, and otherwise use a trusted repository and
a safe working environment.

File tools accept absolute paths anywhere the OS permits, including external
worktrees and temporary directories. Relative paths (including `..`) always use
the selected workspace as their base, even after a shell command changes its
working directory. `list_files` and `grep` default to the workspace and return
workspace-relative paths, including `../` paths for external results. Protected
patterns such as `.git/*`, `.env`, `.env.*`, `*.pem`,
`*.key`, and `**/secrets*` remain read-only through file tools at any depth.

The built-in `worker` sub-agent is general-purpose: it can inspect, edit files,
run commands and tests, and use the same extension tools, native/local web tools,
and currently enabled MCP servers as the main agent. It inherits the repository
instructions, file protections, and extension guardrails, not a separate read-only
policy. Shell commands can read or modify anything the OS allows, including files
protected by file tools. Repository instruction discovery remains scoped to the
selected workspace and its configured ancestors, not every external path the
tools can access.

Use `delegate_task` with `agent_name="worker"` and a self-contained task. Workers
share workspace files, so give concurrent workers non-overlapping edits. Each has
fresh conversation context and its own shell and plan; the parent's conversation
is not copied. Recursive delegation is disabled. Each task retains a separate
120-request budget and a 15-minute timeout. Specialized extension delegates keep
their own tool configuration rather than automatically gaining the worker's tools.

Harness is pinned to upstream commit
[`12bce878da99bca61a5d8d798bff0a3bc93bd153`](https://github.com/pydantic/pydantic-ai-harness/commit/12bce878da99bca61a5d8d798bff0a3bc93bd153),
which is newer than the 0.31.0 release. The pin is a direct dependency, so both
`uv sync` and `make install` use it. Coder supplies `read_file`, `write_file`,
`edit_file`, `list_files`, `grep`, and `shell`; pcode adds planning, the worker,
and optional web search. `list_files` and `grep` use the bundled ripgrep and
respect ignore rules. Edits support either one replacement pair or a
`replacements` array, validated before a single write. Models mangle that nested
array often enough to matter — roughly one call in ten arrives as a JSON string
and is rejected with `Input should be a valid array` — so pcode's file-tool
instructions reserve it for two or more edits to one file and steer single edits
to the flat `old_text`/`new_text` pair, which has no such failure mode.

The default upstream `shell` accepts unrestricted commands: treat it as arbitrary
code execution as the invoking user. Foreground calls wait up to 270 seconds
(or a shorter requested timeout), then return the PID and output/status paths
without killing a still-running command. Background mode returns those handles
immediately. Read the returned files to inspect progress and use the returned
process-group stop command to terminate it. Processes and raw output logs can
outlive the turn and pcode itself; `--no-save` does not disable these logs.
Cancelling a call while it is waiting terminates its process group, but cancelling
a later turn does not stop a command whose handles were already returned.
Files and code returned by tools are sent to the selected model.

## Web search

The coder can search the web and read pages. Search and page fetching are
separate because provider-native search returns snippets only; reading
documentation needs the fetch either way. Each picks the best backend available:

| | Search | Fetch a URL |
| --- | --- | --- |
| Model has a native tool (Anthropic, OpenAI) | provider runs it server-side | Anthropic runs it server-side |
| `EXA_API_KEY` set | Exa `web_search` | Exa `get_page` |
| Otherwise | DuckDuckGo `web_search` | HTTP fetch `get_page`, converted to Markdown |

Native tools are billed by the provider per search; the Exa key is read by the
Exa client and never passed to the model. Search returns up to five results;
page retrieval returns up to 10,000 characters. Queries, URLs, and returned
content go to whichever backend is in use, reach the model, and can be saved in
session history. The worker inherits the same web policy and tools.

```sh
pcode config set web_search local   # Never advertise native tools to the model
pcode config set web_search off     # No web tools at all
pcode config unset web_search       # Back to auto
```

`local` is the escape hatch for an endpoint that rejects server-side tools.
Changes apply on `/reload` or the next launch. This is all one bundled
extension, `web_research`; copy `src/pcode/extensions/web_research.py` to
`~/.config/pcode/extensions/web_research.py` to change backends, limits, or
instructions, or leave its `setup` empty to remove the tools.

## Browser (per conversation)

`/browser launch` gives the model your installed Chrome, through Harness's
[Playwright tools](https://pydantic.dev/docs/ai/harness/playwright/): navigate,
click, type, snapshot, screenshot, and the rest, plus `browser_open()`, which
brings the window to the front, and `browser_tabs()`. When a page needs you to
sign in, the model leaves it on screen and asks; log in there and tell it when
you are done. A `browser` sub-agent shares the same window, so a multi-step
task can run without every page landing in the main context. `/browser off`
quits that Chrome and removes the tools; a fresh pcode starts with them off.

Chrome is started by pcode with a debugging port and its own profile under
`~/.local/state/pcode/chrome`, apart from your everyday one, and driven over CDP.
That is what lets Google and similar sign-in pages accept it: Playwright's own
Chromium launches flagged as automated and they refuse it. The profile persists,
so a site you log in to once stays logged in for later pcode sessions; delete
the directory to forget everything. Set `PCODE_BROWSER_CHROME` to pick the
binary. With no Chrome installed it falls back to Playwright's Chromium,
downloaded on first use.

`/browser attach` joins the Chrome you already have open instead, logins
included, so nothing needs signing in to. The model works in a tab of its own,
and `browser_tabs()` shows it what you have open, so "check my email" finds the
mail tab and opens that site rather than guessing. Chrome only exposes itself
once remote debugging is on: the first `/browser attach` opens
`chrome://inspect/#remote-debugging` in your Chrome for you to flip the switch,
then run it again. (Starting Chrome with `--remote-debugging-port` works too.)
pcode finds the port from Chrome's `DevToolsActivePort` file, or from
`PCODE_BROWSER_CDP_URL` / `PCODE_BROWSER_PORT_FILE`. pcode opens its own tab
there and closes it on `/browser off`, never quitting your Chrome. This is the
higher-risk mode: the model can act as every account that browser is signed in
to.

| `/browser …` | Does |
| --- | --- |
| `launch` | Open pcode's own Chrome window, with its own persistent logins |
| `attach` | Join the Chrome you have open, your logins included |
| `off` | Close the browser (or pcode's tab in yours) and remove the tools |
| `status` | Show which browser is in use and where it is |

The window is visible and localhost is reachable, since a dev server is the
usual target. The trade-off of turning it on at all: any page the model reads
can tell it to act with your login, and nothing enforces otherwise beyond you
watching the window. This is the bundled `browser` extension; a user file of the
same name replaces it.

## Code mode (opt-in)

[Code mode](https://pydantic.dev/docs/ai/harness/code-mode/) replaces individual
tool calls with a single sandboxed Python snippet, so the model can fan out
lookups with `asyncio.gather`, filter results, and return only what matters
without a model turn per dependent batch.

```sh
pcode config set code_mode on   # Applies on next launch
pcode config unset code_mode    # Back to plain tool calling
```

Only read-only lookups are sandboxed: `read_file`, `list_files`, `grep`,
`read_tool_result`, `web_search`, and `get_page`. Edits, plan updates, the
persistent shell, and delegation keep issuing their own tool calls, so diffs,
command previews, and the plan panel still show what happened rather than an
opaque snippet that did it. A `run_code` call is displayed by the calls the
snippet makes and its size (`grep · read_file ×2 · 12 lines`); the snippet
itself is visible in the tool-call inspector.

The snippet also streams into the pinned preview box as the model writes it,
titled `Preparing code · not yet run`, in the same place edit diffs and command
output appear. Only complete lines are shown, the box clears the moment the
snippet is dispatched, and the text never enters the transcript — it is a
pending argument, not a result. `/show-edits off` hides it along with edit previews.

Snippets run in the Monty sandbox with no host filesystem or environment of their
own: pcode passes no `mount` or `os_access`, so the only way out is the sandboxed
tools, which enforce the same workspace rules as ever. Harness caps each snippet
at 30 seconds and 256 MiB of heap.
