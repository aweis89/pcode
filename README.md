# pcode

A small, streaming terminal for a Pydantic AI Coder agent, with an offline UI
preview. Full documentation lives in [`docs/`](docs/index.md); see
[PLAN.md](PLAN.md) for the longer-term direction.

## Install

With [Homebrew](https://brew.sh/):

```sh
brew tap aweis89/pcode https://github.com/aweis89/pcode.git
brew install --HEAD aweis89/pcode/pcode
```

Or from a checkout with [uv](https://docs.astral.sh/uv/):

```sh
uv run pcode -m openai-codex:gpt-5.6-luna   # this checkout only
uv tool install --editable .                # bare `pcode` everywhere
```

## Quick start

```sh
codex login                                  # or /login [anthropic|openai-codex] inside pcode, or export a provider API key
pcode -m openai-codex:gpt-5.6-luna           # interactive; /model (Ctrl+L) saves a default
pcode                                        # reuses the saved model, or opens the offline preview
pcode -C /path/to/repo "Summarize the open TODOs"
git diff | pcode -p --no-save                # non-interactive: reply to stdout
pcode --continue                             # resume this directory's newest session
pcode --theme-preview                        # offline sample output and the style gallery
```

**Live mode edits files and runs shell commands with your permissions and no
approval prompt.** Read [tool permissions](docs/tools.md#tool-permissions)
before pointing it at anything you care about.

## Documentation

| Page | What it covers |
| --- | --- |
| [Getting started](docs/getting-started.md) | Homebrew and source installs, `-C`, `--print`, shell completion |
| [Providers and models](docs/providers.md) | Authentication, supported providers, the model picker, reasoning effort, Meridian, proxies |
| [Configuration](docs/configuration.md) | `pcode config`, per-repository overrides, trusting repository code, the settings table, syntax styles |
| [Commands and keys](docs/commands.md) | Slash commands, key bindings, vi mode, tmux newlines, status line, `!command`, the diff and tool inspectors |
| [Tools](docs/tools.md) | Tool permissions, web search, the browser, code mode |
| [MCP servers](docs/mcp.md) | Opt-in MCP configuration, OAuth sign-in, deferred tool search |
| [Working in a repository](docs/workspace.md) | `AGENTS.md`/`CLAUDE.md`, skills as slash commands, one worktree per session |
| [Sessions and recovery](docs/sessions.md) | Saving, resuming, recalling earlier sessions, checkpoints, retries |
| [Context, limits and caching](docs/context.md) | Prompt overhead, compaction, output limits, prompt cache warnings |
| [The transcript](docs/transcript.md) | What lands in scrollback: diffs, thinking, errors, command output, `/redraw` |
| [Development](docs/development.md) | Tests, architecture, profiling, references |

Deeper notes: [prompt caching](docs/prompt-caching.md),
[conversation tree](docs/conversation-tree.md), [profiling](docs/profiling.md),
[dependencies](docs/dependencies.md), [Meridian validation](docs/meridian-validation.md).

The same pages build into a browsable site with `make docs-serve`; `make docs`
checks every page and anchor link.

## Contributing

```sh
make test        # fast suite; real-tmux regressions skipped
make test-all    # everything, before touching layout, streaming, the editor, or the prompt
```

[AGENTS.md](AGENTS.md) has the worktree workflow and the traps worth knowing
before editing.
