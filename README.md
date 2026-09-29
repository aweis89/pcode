# pcode

A coding agent for people who live in the terminal. It streams into your
terminal's normal scrollback instead of taking over the screen, keeps every
conversation so you can resume, search or fork it, and runs on the model
subscription you already pay for.

**Documentation: [aweis89.github.io/pcode](https://aweis89.github.io/pcode/)**

- Scrollback you can re-render after the fact: show every command and diff
  while it runs, fold them to summaries when it's done.
- `/tools` shows every command the agent ran and its full output.
- `/tree` rewinds and forks the conversation at any point.
- A shell built for slow work: long commands become background jobs, and a
  finished job wakes the agent. Good for watching CI and fixing what fails.
- Your Claude subscription through Anthropic's own Agent SDK and Claude Code
  login, or your ChatGPT one, or any API key.
- A git worktree per session, so several agents can work on one repo at once.
- `/btw` side questions, background sessions, recall of past sessions, a
  browser the agent can drive, and Python extensions.

See [PLAN.md](PLAN.md) for the longer-term direction.

## Install

With [Homebrew](https://brew.sh/):

```sh
brew tap aweis89/pcode https://github.com/aweis89/pcode.git
brew install --HEAD aweis89/pcode/pcode
```

Or from a checkout with [uv](https://docs.astral.sh/uv/):

```sh
uv run pcode -m openai-codex:gpt-5.6-luna   # this checkout only
uv tool install --editable '.[claude]'      # bare `pcode` everywhere; drop [claude] to skip claude: models
```

## Quick start

```sh
codex login                                  # or /login [claude|openai-codex] inside pcode, or export a provider API key
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
| [Providers and models](docs/providers.md) | Authentication, supported providers, the model picker, reasoning effort, Claude Code, Meridian, proxies |
| [Configuration](docs/configuration.md) | `pcode config`, per-repository overrides, trusting repository code, the settings table, syntax styles |
| [Commands and keys](docs/commands.md) | Slash commands, key bindings, vi mode, tmux newlines, status line, `!command`, the diff and tool inspectors |
| [Tools](docs/tools.md) | Tool permissions, web search, the browser, code mode |
| [MCP servers](docs/mcp.md) | Opt-in MCP configuration, OAuth sign-in, deferred tool search |
| [Working in a repository](docs/workspace.md) | `AGENTS.md`/`CLAUDE.md`, skills as slash commands, one worktree per session |
| [Sessions and recovery](docs/sessions.md) | Saving, resuming, recalling earlier sessions, checkpoints, retries |
| [Context, limits and caching](docs/context.md) | Prompt overhead, compaction, output limits, prompt cache notices |
| [The transcript](docs/transcript.md) | What lands in scrollback: diffs, thinking, errors, command output, `/redraw` |
| [Conversation tree](docs/conversation-tree.md) | `/tree`: rewinding and forking a conversation |
| [Side questions](docs/side-questions.md) | `/btw`: asking about the running turn without interrupting it |

The same pages build into a browsable site with `make docs-serve`; `make docs`
checks every page and anchor link.

## Contributing

```sh
make test        # fast suite; real-tmux regressions skipped
make test-all    # everything, before touching layout, streaming, the editor, or the prompt
```

[AGENTS.md](AGENTS.md) has the worktree workflow and the traps worth knowing
before editing. Contributor notes (architecture, dependencies, profiling, prompt
caching, provider design) live in [`dev/`](dev/), outside the published docs.
