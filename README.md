<img width="1360" alt="pcode fixing a bug: a word-level diff of the edit, then a worker sub-agent reviewing it with its own plan nested under the task list" src="https://raw.githubusercontent.com/cruxwell/pcode/master/docs/assets/screenshots/readme.png" />

# pcode

pcode is a terminal-native coding agent built for long-running and parallel
work. Background commands wake the agent when they finish, every session can
use its own git worktree, conversations can be rewound and forked, and every
tool call stays inspectable.

- **Slow work doesn't block you.** Long commands become background jobs, and a
  finished job wakes the agent, so it can watch tests or CI and fix what fails.
- **Agents in parallel.** A git worktree per session lets several agents work
  on one repo at once.
- **Rewind and fork.** `/tree` returns to any point in a conversation and
  branches from there. Every session is kept, so you can resume or search it.
- **Nothing hidden.** `/tools` shows every command the agent ran and its full
  output, and scrollback re-renders to show or fold every command and diff.

It runs on any model [Pydantic AI](https://ai.pydantic.dev/) supports, or on
your Claude or ChatGPT subscription.

**Documentation: [cruxwell.github.io/pcode](https://cruxwell.github.io/pcode/)**

## Install

With [Homebrew](https://brew.sh/):

```sh
brew tap cruxwell/pcode https://github.com/cruxwell/pcode.git
brew install --HEAD cruxwell/pcode/pcode
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
approval prompt.** Read [tool permissions](https://cruxwell.github.io/pcode/tools/#tool-permissions)
before pointing it at anything you care about.

## More

- **Your subscription:** Claude runs through Anthropic's own Agent SDK and
  Claude Code login, the way Anthropic supports, and ChatGPT through a Codex
  login. See [providers](https://cruxwell.github.io/pcode/providers/).
- [Email remote control](https://cruxwell.github.io/pcode/email/): send a task
  from Gmail on your phone, reply to keep going, and take the session over at a
  terminal with `pcode --attach`.
- [Custom keybindings and Vim editing](https://cruxwell.github.io/pcode/keybindings/):
  map keys to commands with arguments, add a Space leader in normal mode, or use
  `jj` to leave insert mode.
- `/btw` side questions, background sessions, recall of past sessions, a
  browser the agent can drive, and Python extensions.
- Pydantic AI's Harness coder capabilities, with the rest of a finished agent
  on top: MCP with OAuth and tool search, searchable sessions, jobs,
  worktrees. Extensions are plain Pydantic AI capabilities. The conversation
  tree follows [pi](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/tree.md)'s.

See [PLAN.md](https://github.com/cruxwell/pcode/blob/master/PLAN.md) for the longer-term direction.

## Documentation

| Page | What it covers |
| --- | --- |
| [Getting started](https://cruxwell.github.io/pcode/getting-started/) | Homebrew and source installs, `-C`, `--print`, shell completion |
| [Scrollback and transparency](https://cruxwell.github.io/pcode/guide/scrollback/) | Guide: what goes into scrollback, `/tools`, `/diffs` |
| [A shell for long-running work](https://cruxwell.github.io/pcode/guide/shell/) | Guide: background jobs, watching CI |
| [Parallel agents](https://cruxwell.github.io/pcode/guide/parallel/) | Guide: worktrees, parallel sub-agents, `/agents` |
| [Extending pcode](https://cruxwell.github.io/pcode/guide/extending/) | Guide: extensions, skills, settings |
| [Providers and models](https://cruxwell.github.io/pcode/providers/) | Authentication, supported providers, the model picker, reasoning effort, Claude Code, Meridian, proxies |
| [Configuration](https://cruxwell.github.io/pcode/configuration/) | `pcode config`, per-repository overrides, trusting repository code, the settings table, syntax styles |
| [Commands and keys](https://cruxwell.github.io/pcode/commands/) | Slash commands, key bindings, vi mode, tmux newlines, status line, `!command`, the diff and tool inspectors |
| [Keybindings](https://cruxwell.github.io/pcode/keybindings/) | Custom command mappings with `/bind`, Ctrl shortcuts, vi editing, a normal-mode leader, and custom escape sequences such as `jj` |
| [Tools](https://cruxwell.github.io/pcode/tools/) | Tool permissions, web search, the browser, code mode |
| [MCP servers](https://cruxwell.github.io/pcode/mcp/) | Opt-in MCP configuration, OAuth sign-in, deferred tool search |
| [Working in a repository](https://cruxwell.github.io/pcode/workspace/) | `AGENTS.md`/`CLAUDE.md`, skills as slash commands, one worktree per session |
| [Sessions and recovery](https://cruxwell.github.io/pcode/sessions/) | Saving, resuming, recalling earlier sessions, checkpoints, retries |
| [Context, limits and caching](https://cruxwell.github.io/pcode/context/) | Prompt overhead, compaction, output limits, prompt cache notices |
| [The transcript](https://cruxwell.github.io/pcode/transcript/) | What lands in scrollback: diffs, thinking, errors, command output, `/redraw` |
| [Conversation tree](https://cruxwell.github.io/pcode/conversation-tree/) | `/tree`: rewinding and forking a conversation |
| [Side questions](https://cruxwell.github.io/pcode/side-questions/) | `/btw`: asking about the running turn without interrupting it |

The same pages build into a browsable site with `make docs-serve`; `make docs`
checks every page and anchor link.

## Contributing

Bugs: [open an issue](https://github.com/cruxwell/pcode/issues/new/choose).
Security problems: see [SECURITY.md](https://github.com/cruxwell/pcode/blob/master/SECURITY.md). Setup and checks are in
[CONTRIBUTING.md](https://github.com/cruxwell/pcode/blob/master/CONTRIBUTING.md).

```sh
make test        # fast suite; real-tmux regressions skipped
make test-all    # everything, before touching layout, streaming, the editor, or the prompt
```

[AGENTS.md](https://github.com/cruxwell/pcode/blob/master/AGENTS.md) has the worktree workflow and the traps worth knowing
before editing. Contributor notes (architecture, dependencies, profiling, prompt
caching, provider design) live in [`dev/`](https://github.com/cruxwell/pcode/tree/master/dev), outside the published docs.
