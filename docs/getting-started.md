# Getting started

## Install with Homebrew

With [Homebrew](https://brew.sh/) installed:

```sh
brew tap aweis89/pcode https://github.com/aweis89/pcode.git
brew install --HEAD aweis89/pcode/pcode
pcode --theme-preview
pcode -m openai-codex:gpt-5.6-luna
```

This repository doubles as a Homebrew tap. The explicit repository URL is
required because its name is `pcode`, not `homebrew-pcode`. There are no tagged
releases yet, so the formula installs the latest `master` with `--HEAD`, rather
than a stable release. These commands become available once `Formula/pcode.rb`
is published to GitHub.

Homebrew installs Python 3.13 and uses `uv` at build time to install the
application and its locked dependencies into a private environment. Installation
requires network access to fetch Python packages; it does not modify your global
Python environment. Run `pcode` directly after installation (no `uv run` needed).
Provider authentication is still required for live models; see
[providers and models](providers.md).

To update or uninstall:

```sh
brew update
brew upgrade --fetch-HEAD aweis89/pcode/pcode
# To remove:
brew uninstall pcode
brew untap aweis89/pcode
```

The formula includes offline smoke tests: `brew test aweis89/pcode/pcode`.
This is an upstream tap, not a formula in `homebrew/core`.

## Run from source

With [uv](https://docs.astral.sh/uv/) installed, from this directory:

```sh
uv run pcode -m openai-codex:gpt-5.6-luna
```

## Pick a workspace and send a first prompt

The current directory is the Coder workspace; select another repository with `-C`:

```sh
uv run pcode -m openai-codex:gpt-5.6-luna -C /path/to/repo
```

For a bare `pcode` command available outside this project:

```sh
uv tool install --editable .
pcode -m openai-codex:gpt-5.6-luna -C /path/to/repo
```

Try asking: `What does this repository do? Read the README and cite relevant files.`
Live conversations save automatically when the first model prompt is submitted.

A prompt on the command line is sent as the first message, then the editor opens
as usual. Add `-p`/`--print` to skip the editor: the reply goes to stdout, tool
activity and errors go to stderr, and the exit status reports whether the turn
succeeded. On a terminal the reply is rendered Markdown, block by block as each
response settles; redirected to a file or a pipe it is the Markdown source,
streamed as it arrives. Without a prompt argument, `--print` reads one from stdin.

```sh
pcode "Summarize the open TODOs in this repo"          # first message, then interactive
pcode -p "Which files handle sessions?" > answer.md    # non-interactive
git diff | pcode -p --no-save                          # prompt from stdin
pcode -p --continue "And the tests for those?"         # continue this directory's latest session
```
Opening the app, using commands, or quitting without a prompt creates no session.
`/new` resets context without deleting the old conversation; its replacement is
created on the next model prompt.

## Shell completion

`pcode --completions SHELL` prints a completion script for `zsh`, `fish`, or
`bash`. It is generated from the argument parser itself, so flags and their
choices (themes, color styles, shells) stay in step with the installed version;
regenerate after upgrading.

```sh
pcode --completions zsh > ~/.zsh/completions/_pcode   # directory must be on $fpath
pcode --completions fish > ~/.config/fish/completions/pcode.fish
echo 'eval "$(pcode --completions bash)"' >> ~/.bashrc
```

The zsh script works either autoloaded from `$fpath` or sourced from `.zshrc`.
