# Writing a pcode extension

An extension is one Python file that defines `setup(pcode)`. pcode imports it
when the agent starts and again on `/reload`, calls `setup`, and wires whatever
it registered into the agent and the terminal.

## Where to put it

| Location | Scope | Loads |
| --- | --- | --- |
| `~/.config/pcode/extensions/<name>.py` (or `$XDG_CONFIG_HOME/pcode/extensions/`) | user, every workspace | always |
| `<workspace>/.pcode/extensions/<name>.py` | this project | only once the user trusts the repository (launch prompt, or `project_extensions on`) |
| `/config set extension_dirs DIR:DIR` | extra directories | always |
| `src/pcode/extensions/` (shipped with pcode) | bundled defaults | always |

A directory with `__init__.py` works too, for multi-file extensions. Names
starting with `_` or `.` are skipped. When two directories hold the same name,
the project one wins, then user, then configured, then bundled. That is how a
bundled default is replaced: a user file named `web_research.py` overrides the
shipped web search and fetch tools, and one whose `setup` does nothing removes
them. The bundled files are ordinary extensions and worth reading as examples.

After writing or changing an extension, run `/reload` (or tell the user to). It
rebuilds the agent around the current conversation and prints each extension's
status; a broken one is reported with its error and line and contributes
nothing, and the session keeps working. `/extensions` lists the current state.

## The API

```python
from pcode.ext import ExtensionAPI


def setup(pcode: ExtensionAPI) -> None: ...
```

`pcode.name` is the file stem, `pcode.workspace` the resolved workspace path.
`pcode.session_dir` is the resolved session-storage root, honoring `--session-dir`,
`PCODE_SESSION_DIR`, and the default state directory.

### Tools the model can call

```python
@pcode.tool
def ticket_status(ticket_id: str) -> str:
    """Look up a ticket's title and status."""
    return subprocess.run(["tk", "show", ticket_id], capture_output=True, text=True).stdout
```

The docstring is the description the model sees; the type hints become the
schema (this is Pydantic AI's `Agent.tool_plain`). `async def` works. Raise
`pydantic_ai.ModelRetry("...")` to send the model a correction instead of a
result.

### Extra instructions

```python
pcode.instructions("Always run `make lint` before declaring a change finished.")
```

Keep this text fixed for the life of the session. It sits in the cached prompt
prefix, so anything that changes between requests (time, git status) breaks
caching on every turn. Read changing state inside a tool or hook instead.

### Hooks on the agent lifecycle

`pcode.hooks.on.<name>` are Pydantic AI's `Hooks` decorators. The ones worth
knowing:

```python
from pydantic_ai import ModelRetry


@pcode.hooks.on.before_tool_execute
async def guard(ctx, *, call, tool_def, args):
    """Runs before every tool call; return args (possibly modified) or raise to block."""
    if tool_def.name == "shell" and "rm -rf" in str(args.get("command", "")):
        pcode.ui.notify(f"Blocked: {args['command']}", "warning")
        raise ModelRetry("Destructive command blocked by the rm-guard extension.")
    return args


@pcode.hooks.on.after_tool_execute
async def log(ctx, *, call, tool_def, args, result):
    """Runs after; return the result (possibly modified)."""
    return result


@pcode.hooks.on.before_model_request
async def before(ctx, request_context):
    return request_context


@pcode.hooks.on.after_run
async def done(ctx, *, result):
    return result
```

Other hook names: `before_run`, `before_model_request`, `after_model_request`,
`before_tool_validate`, `after_tool_validate`, `tool_execute` (wrap: receives
`handler` and must call it), `tool_execute_error`, `event` (every stream
event), `prepare_tools` (filter or rewrite the tool definitions the model
sees). Each must return what it was given unless it means to change it.

Tool names in this agent: `read_file`, `write_file`, `edit_file`, `list_files`,
`grep`, `shell`, `write_plan`, `delegate_task`, plus any MCP or extension tools.

### Slash commands

```python
def standup(argument: str) -> None:
    log = subprocess.run(
        ["git", "log", "--since=yesterday", "--oneline"], capture_output=True, text=True
    )
    pcode.ui.notify(log.stdout or "No commits since yesterday.")


pcode.register_command("/standup", "Summarize commits since yesterday", standup)
pcode.register_command(
    "/mode",
    "Set a mode",
    set_mode,
    arguments=("fast", "careful"),
    argument_descriptions={"fast": "Skip verification", "careful": "Verify every step"},
)
```

Handlers run on the terminal's event loop, so keep them quick. Raise
`ValueError("message")` to show a usage error. A name pcode already uses is
reported and skipped. Commands cannot send a prompt to the model; to do that,
write a skill (`SKILL.md`) instead.

### Sub-agents

```python
from pydantic_ai import Agent

reviewer = Agent(name="reviewer", description="Review a diff for bugs", instructions="...")
pcode.subagent(reviewer, timeout_seconds=600)
```

The agent is listed beside the explorer under `delegate_task`. Leave its model
unset to run on the session's model; keyword options are Harness `SubAgent`
fields (`usage_limits`, `timeout_seconds`, `max_calls`). Give it capabilities
of its own; the parent's tools are not inherited.

### Notices and lifecycle

`pcode.ui.notify(text, level="info" | "warning" | "error")` prints a transient
line in the transcript. Safe to call from tools, hooks, and commands.

`pcode.ui.request_reload()` asks for `/reload` once the terminal is idle, for a
command that changes what `setup` contributes (see the bundled `browser.py`,
whose `/browser launch` adds tools). It raises `ValueError` mid-turn, so call it
before changing state. State that must survive the reload cannot live in the
extension module, which is re-imported.

`@pcode.on_close` registers an `async` function run when the terminal exits,
for a process or connection a tool started.

### Anything else

```python
pcode.add_capability(SomeCapability(...))
```

Any Pydantic AI `AbstractCapability` subclass, or a Harness capability, can be
added directly. This is the full power of the system; the helpers above are
shortcuts to it. The capability gets `id="ext.<name>"` if it has none, so
`/status` can attribute its prompt cost.

## Rules of thumb

- Do import work inside functions when it is slow or optional; `setup` runs on
  every start and reload.
- Do not start background processes or threads in `setup`; start them lazily
  from the tool or command that needs them.
- Extensions run with the user's full permissions, in-process. There is no
  sandbox.
- Standard library and everything pcode depends on (`pydantic_ai`,
  `pydantic_ai_harness`, `rich`, `httpx`) are importable. Other packages are
  not unless installed into pcode's environment.
- Test the file imports cleanly before `/reload`: `python -c "import runpy;
  runpy.run_path('path/to/ext.py')"` catches syntax errors early.

## Complete example

`~/.config/pcode/extensions/protect_env.py`:

```python
"""Refuse to edit secret files and offer a /secrets command to list them."""

from fnmatch import fnmatch

from pydantic_ai import ModelRetry

PROTECTED = (".env", ".env.*", "*.pem", "*.tfvars")
EDIT_TOOLS = {"write_file", "edit_file"}


def setup(pcode):
    def protected(path: str) -> bool:
        name = path.rsplit("/", 1)[-1]
        return any(fnmatch(name, pattern) for pattern in PROTECTED)

    @pcode.hooks.on.before_tool_execute
    async def guard(ctx, *, call, tool_def, args):
        if tool_def.name in EDIT_TOOLS and protected(str(args.get("path", ""))):
            pcode.ui.notify(f"Refused to edit {args['path']}", "warning")
            raise ModelRetry(f"{args['path']} holds secrets; ask the user to edit it.")
        return args

    pcode.instructions("Never write to .env, key, or tfvars files; ask the user instead.")

    pcode.register_command(
        "/secrets",
        "Show which file patterns are write-protected",
        lambda _: pcode.ui.notify("Protected: " + ", ".join(PROTECTED)),
    )
```
