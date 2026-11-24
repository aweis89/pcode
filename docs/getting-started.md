# Getting started

## Install

pcode requires Python 3.14 or newer. The installers below use Python 3.15;
CI tests both on Linux and macOS.

With [Homebrew](https://brew.sh/):

```sh
brew install aweis89/tap/pcode
```

That installs the latest release; add `--HEAD` to track `master` instead. Homebrew
installs pcode and its dependencies into a private environment without touching
your global Python, and adds `shfmt` for nicer command formatting in `/tools`
and [delta](https://dandavison.github.io/delta/) for
[richer diffs](transcript.md#diffs-with-delta).

To update or remove it:

```sh
brew update && brew upgrade aweis89/tap/pcode   # --fetch-HEAD for a --HEAD install
brew uninstall pcode && brew untap aweis89/tap
```

### With uv

pcode is on [PyPI](https://pypi.org/project/pcode/), so
[uv](https://docs.astral.sh/uv/) can install it into its own environment:

```sh
uv tool install --python 3.15 'pcode[claude]'    # `claude` adds Claude subscription support
uv tool upgrade pcode
uv tool uninstall pcode
```

Leave out `[claude]` if you don't use a Claude subscription: it bundles the
Claude Code CLI, over 200 MB installed. Homebrew also adds `shfmt` and delta, which uv
doesn't; install them yourself if you want them.

To install from a checkout instead, see [run from source](#run-from-source).

## Sign in and pick a model

Use a subscription you already have, or any provider API key:

```sh
pcode -m claude:claude-sonnet-5          # Claude subscription, via Claude Code's own login
pcode -m antigravity:gemini-4-neon       # Antigravity subscription, via the `agy` CLI
pcode -m openai-codex:gpt-5.6-luna       # ChatGPT subscription
pcode -m anthropic:claude-sonnet-5       # with ANTHROPIC_API_KEY set; other providers work the same way
```

If you're not signed in yet, run `/login claude`, `/login antigravity`
(requires the `agy` CLI), or `/login openai-codex` inside pcode; each signs in
through the provider's own flow in your browser. Ctrl+L (or `/model`) opens a
model picker at any time, and the model you pick is saved, so later a bare
`pcode` is enough. See [providers and models](providers.md) for every provider.

## Your first session

pcode works in the current directory. Start it in a repository, or point it at
one with `-C`:

```sh
cd ~/src/my-app && pcode
pcode -C ~/src/my-app "What does this repository do? Cite the relevant files."
```

A prompt on the command line is sent as the first message, then the editor
opens as usual. You can start typing straight away, before the editor has
finished loading. If pcode asks whether to trust the repository, that's about
running code the repository ships (its extensions and setup scripts); see
[trusting a repository's own code](configuration.md#trusting-a-repositorys-own-code).

!!! warning "The agent acts with your permissions"
    There is no approval prompt: the agent edits files and runs commands as
    you. Turn on the opt-in OS-level sandbox anytime with
    `/extensions on sandbox` ([write policy and shell sandbox](tools.md#write-policy-and-shell-sandbox-opt-in)),
    and see [tool permissions](tools.md#tool-permissions).

A few things worth knowing on day one:

- **Enter while the agent is working** steers the running turn with your
  message. Ctrl+C cancels it.
- **Ctrl+G** cycles where command output shows: live in the activity panel
  while it runs, then also kept in scrollback, then back to summaries. See
  [scrollback and transparency](guide/scrollback.md).
- **`/tools`** shows every call the agent made, with its full output.
- **`/diffs`** reviews the session's git diff and lets you leave inline notes
  that go straight back into your prompt.
- **`/btw QUESTION`** asks about the running turn without interrupting it.
- **`pcode --theme-preview`** renders an offline sample transcript and style
  gallery so you can pick a theme or check colors without calling a model.
- **`/help`** lists every command.

For Shift+Enter newlines, tmux configuration, and 24-bit color (which the
default `frame_style shaded` relies on), see [terminal setup](terminal.md).

Conversations save automatically once you send the first prompt.
`pcode --continue` resumes the latest one in this directory, and `/resume`
browses them all.

## Pick a workspace with `--launch`

`pcode --launch` lists the projects you have sessions in, newest first, with
how many sessions each has and how many are running in the background. It's for
the moments you aren't in a project yet, such as a terminal window that just
opened in your home directory.

```text
pcode

› ~/p/pcode     12 sessions · 2 running · 5m ago
  ~/p/website   3 sessions · 2d ago

enter new session  c continue  o open folder  q quit
```

- **Enter** starts a new session in the selected project.
- **`c`** continues the project's newest session, even one that ran in a
  worktree or subdirectory, attaching if it's running in a
  [background host](sessions.md#background-sessions).
- **`o`** asks for any other folder. Tab completes, and a folder dragged in
  from Finder works too.

The current directory is listed first unless it's your home directory.
Sessions from a git worktree count toward their repository. Sessions that never
got a prompt aren't counted, and scratch directories under `/tmp` or the system
temp folder are left out unless you start from one. With no sessions yet, the
screen says so and **Enter** asks for a folder.

If direnv is installed and an `.envrc` applies to the project, pcode starts
through `direnv exec`, so the project's variables are there even though no shell
prompt ever loaded them. It does the same when the shell you launched from has
another project's variables loaded, so they don't leak across. A blocked
`.envrc` stops the launch with direnv's message; run `direnv allow` there.

`--model`, `--host`, `--worktree`, `--no-save` and `--session-dir` pass through
to the session it starts (only `--host` and `--session-dir` when continuing,
since a session keeps its own model).

To make it what a terminal opens with, use `pcode --launch shell` as the
terminal's launch command. That adds **`s`** for a plain shell in the selected
project, and leaves you in a login shell there when pcode quits (even by
Ctrl-C) instead of closing the window. Quitting the picker gives you a shell
where you started. In Ghostty:

```ini
initial-command = /bin/zsh -lc 'pcode --launch shell'
```

## Recommended: a worktree per session

If you'll run more than one session on a repository, give each its own git
worktree so they can't trample each other's edits:

```sh
pcode config project set worktree on   # this repository
pcode config set worktree on           # every repository
```

See [parallel agents](guide/parallel.md).

## Scripting with `--print`

`-p`/`--print` skips the editor: the reply goes to stdout, tool activity and
errors to stderr, and the exit status says whether the turn succeeded. Without
a prompt argument it reads one from stdin.

```sh
pcode -p "Which files handle sessions?" > answer.md
git diff | pcode -p --no-save "Review this diff"
pcode -p --continue "And the tests for those?"
```

On a terminal the reply is rendered Markdown; piped or redirected it's the
Markdown source, streamed as it arrives. With `--attach`, `--print` sends the
prompt to a running background session instead; see
[scripting a running host](sessions.md#scripting-a-running-host).

## Shell completion

`pcode --completions SHELL` prints a completion script for `zsh`, `fish`, or
`bash`. It's generated from the installed version, so regenerate after
upgrading. `--attach` completes the IDs of running sessions.

```sh
pcode --completions zsh > ~/.zsh/completions/_pcode   # directory must be on $fpath
pcode --completions fish > ~/.config/fish/completions/pcode.fish
echo 'eval "$(pcode --completions bash)"' >> ~/.bashrc
```

## Run from source

With [uv](https://docs.astral.sh/uv/), from a checkout:

```sh
make install                                           # lockfile-constrained editable install as `pcode` everywhere
uv run pcode -m openai-codex:gpt-5.6-luna              # or run directly from this checkout
```

`make install` constrains dependencies to `uv.lock`, whereas a bare
`uv tool install --python 3.15 --editable '.[claude]'` resolves dependencies
afresh from PyPI. The `claude` extra adds
[`claude:` models](providers.md#claude-code-provider) and bundles the Claude
Code CLI (over 200 MB installed). Leave it out with `make install EXTRAS=` (or
`uv tool install --python 3.15 --editable .`) if you don't need them. Install
`shfmt` yourself for formatted commands in `/tools`; without it, pcode uses a
simpler built-in formatter. Likewise, install
[delta](https://dandavison.github.io/delta/) for
[richer diffs](transcript.md#diffs-with-delta); without it, diffs use Rich.
