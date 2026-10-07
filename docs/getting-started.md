# Getting started

## Install

With [Homebrew](https://brew.sh/):

```sh
brew tap cruxwell/pcode https://github.com/cruxwell/pcode.git
brew install --HEAD cruxwell/pcode/pcode
```

The repository doubles as its own tap, which is why the URL is needed. There
are no tagged releases yet, so `--HEAD` installs the latest `master`. Homebrew
installs pcode and its dependencies into a private environment without touching
your global Python, and adds `shfmt` for nicer command formatting in `/tools`
and [delta](https://dandavison.github.io/delta/) for
[richer diffs](transcript.md#diffs-with-delta).

To update or remove it:

```sh
brew update && brew upgrade --fetch-HEAD cruxwell/pcode/pcode
brew uninstall pcode && brew untap cruxwell/pcode
```

To install from a checkout instead, see [run from source](#run-from-source).

## Sign in and pick a model

Use a subscription you already have, or any provider API key:

```sh
pcode -m claude:claude-sonnet-5          # Claude subscription, via Claude Code's own login
pcode -m openai-codex:gpt-5.6-luna       # ChatGPT subscription
pcode -m anthropic:claude-sonnet-5       # with ANTHROPIC_API_KEY set; other providers work the same way
```

If you're not signed in yet, run `/login claude` or `/login openai-codex`
inside pcode; both sign in through the provider's own flow in your browser.
Ctrl+L (or `/model`) opens a model picker at any time, and the model you pick
is saved, so later a bare `pcode` is enough. See
[providers and models](providers.md) for every provider.

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
    you. See [tool permissions](tools.md#tool-permissions).

A few things worth knowing on day one:

- **Enter while the agent is working** steers the running turn with your
  message. Ctrl+C cancels it.
- **Ctrl+G** shows every command and its output in scrollback; press it again
  to fold them back to summaries. See
  [scrollback and transparency](guide/scrollback.md).
- **`/tools`** shows every call the agent made, with its full output.
- **`/btw QUESTION`** asks about the running turn without interrupting it.
- **`/help`** lists every command.

Conversations save automatically once you send the first prompt.
`pcode --continue` resumes the latest one in this directory, and `/resume`
browses them all.

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
uv run pcode -m openai-codex:gpt-5.6-luna      # this checkout only
uv tool install --editable '.[claude]'         # a bare `pcode` everywhere (or `make install`)
```

The `claude` extra adds [`claude:` models](providers.md#claude-code-provider)
and bundles the Claude Code CLI (about 215 MB). Leave it out with
`uv tool install --editable .` or `make install EXTRAS=` if you don't need
them. Install `shfmt` yourself for formatted commands in `/tools`; without it,
pcode uses a simpler built-in formatter. Likewise, install
[delta](https://dandavison.github.io/delta/) for
[richer diffs](transcript.md#diffs-with-delta); without it, diffs use Rich.
