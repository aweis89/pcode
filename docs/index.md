# pcode

A small, streaming terminal for a Pydantic AI Coder agent, with an offline UI
preview. It runs the Harness Coder tool loop against your checkout, renders the
reply as Markdown in normal scrollback, and saves every conversation so it can
be resumed, searched, or forked later.

```sh
brew tap aweis89/pcode https://github.com/aweis89/pcode.git
brew install --HEAD aweis89/pcode/pcode
pcode -m openai-codex:gpt-5.6-luna
```

!!! warning "Live mode edits files and runs commands"
    There is no approval prompt: the agent edits files and runs shell commands
    with your permissions. Read [tool permissions](tools.md#tool-permissions)
    before pointing it at anything you care about.

## Using pcode

- [Getting started](getting-started.md): install with Homebrew or from source,
  pick a workspace with `-C`, run non-interactively with `--print`, and set up
  shell completion.
- [Providers and models](providers.md): authentication, every supported
  provider, the `/model` picker, reasoning effort, a local Meridian proxy, and
  routing model traffic through an HTTP proxy.
- [Configuration](configuration.md): `pcode config`, per-repository overrides,
  trusting a repository's own code, the full settings table, and code
  highlighting styles.
- [Commands and keys](commands.md): every slash command, key bindings, vi mode,
  newlines under tmux, the status line, `!command`, and the diff and tool
  inspectors.

## What the agent can do

- [Tools](tools.md): what runs without approval, web search, driving your
  browser, and code mode.
- [MCP servers](mcp.md): explicit opt-in servers, OAuth sign-in, and deferred
  tool search.
- [Working in a repository](workspace.md): `AGENTS.md`/`CLAUDE.md`
  instructions, skills as slash commands, and one git worktree per session.

## Sessions, context and output

- [Sessions and recovery](sessions.md): where conversations are stored,
  resuming, recalling earlier sessions, checkpoints, and retries.
- [Context, limits and caching](context.md): what the fixed prompt costs,
  compaction, model and tool output limits, and prompt cache notices.
- [The transcript](transcript.md): what lands in scrollback and how to
  regenerate it.

## Internals

- [Development](development.md): tests, architecture, profiling, references.
- Design notes: [prompt caching and plan reminders](prompt-caching.md),
  [conversation tree](conversation-tree.md), [profiling](profiling.md),
  [dependencies](dependencies.md), [Meridian validation](meridian-validation.md).
