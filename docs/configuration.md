# Configuration

## Global configuration

Global defaults are shared across workspaces in `~/.config/pcode/preferences.json`
(or `$XDG_CONFIG_HOME/pcode/preferences.json`). Inspect and edit them without
opening a terminal UI or connecting a model:

```sh
pcode config                     # List effective startup defaults as JSON
pcode config diff                # List only settings that differ from defaults
pcode config path                # Print the resolved config path
pcode config get theme
pcode config set theme light
pcode config set autocompact on
pcode config set effort high
pcode config set model openai-codex:gpt-5.6-luna
pcode config unset model          # Remove saved model; return to offline preview
pcode config unset theme          # Restore automatic theme detection
pcode config reset                # Remove every saved default at once
```

The same commands are available inside pcode as `/config`, with tab completion:
`/config set theme light`, `/config get autocompact`, `/config unset effort`, etc.
**Config edits affect the next launch, not the running conversation.** To change
an active setting and save its default immediately, use `/theme`, `/effort`,
`/model`, or `/autocompact` instead. CLI overrides such as `--theme` and `--model`
do not rewrite global defaults, and resumed sessions retain their own model.

## Model provider filter

Limit the `/model` selector to specific providers:

```sh
pcode config set model_providers openai-codex,anthropic
pcode config unset model_providers # Restore automatic detection of all active providers
```

Or run `/config set model_providers openai-codex,anthropic` inside pcode.
This setting is read each time the selector opens, without a restart. In
`preferences.json` it is a string: `"model_providers": "openai-codex,anthropic"`.
Names match exactly: `openai-codex` does not include `openai`, `openai-chat`, or
`openai-responses`. Unknown provider names are rejected.

An unset or empty value shows all automatically detected active providers.
A nonempty list filters those providers; it does not configure credentials or
force inactive providers to appear. The current provider is also hidden if it
is excluded. This only filters the selector, not explicit `/model PROVIDER:MODEL`
commands, CLI model overrides, or resumed sessions.

## Independent instances

`PCODE_CONFIG_DIR` points pcode at a different config directory without moving
the rest of your `XDG_CONFIG_HOME`. Everything pcode keeps there follows it:
`preferences.json`, `credentials.json` and `mcp-credentials.json` (so each
instance has its own `/login`), `mcp.json`, `extensions/`, and `worktree-setup`.
Sessions and other state still live under `XDG_STATE_HOME`; set
`PCODE_SESSION_DIR` too if those should be separate.

```sh
PCODE_CONFIG_DIR=~/.config/pcode-work pcode      # separate login and settings
```

`PCODE_CREDENTIALS_FILE` and `PCODE_MCP_CONFIG` still win over the directory
for their single file.

## Per-repository overrides

A workspace's `.pcode/preferences.json` is layered over the user file at launch,
so a setting can hold for one repository and be committed for everyone who
clones it. `config list` and `config get` show the merged result;
`config project` edits the repository file (from `-C DIR` or the current
directory):

```sh
pcode config project set worktree on    # this repo only; writes .pcode/preferences.json
pcode config project list               # the raw overlay
pcode config project unset worktree
pcode config project reset              # Drop the whole overlay
```

A cloned repository must not be able to run code or pick credentials on your
behalf, so `project_extensions`, `trusted_projects`, `extension_dirs`,
`meridian_managed`, and `anthropic_auth` are user-only: the project file cannot
set them, and pcode says so at launch if it tries. The overlay is read from the
launch workspace before any worktree is created, so `worktree on` in a
repository's file is what starts each of its sessions in a worktree.

## Trusting a repository's own code

A repository can ship code that runs at launch with your permissions:
`.pcode/extensions/*.py` (see `/extensions`, which lists every extension and its
state, and turns one on or off) and `.pcode/worktree-setup`. Neither
runs until you trust that repository. The first interactive launch inside one
that ships either asks:

```
pcode: /path/to/repo ships code that runs at launch with your permissions: .pcode/worktree-setup
Trust this repository? [y/N]
```

`y` records the repository's primary checkout in `trusted_projects` (so all of
its worktrees are covered) and never asks again; `n` skips the code for this
launch and asks next time. `--print` has nobody to ask, so it skips and says so.
Revoke with `pcode config unset trusted_projects` (or edit the `:`-separated
list). `pcode config set project_extensions on` trusts every repository, which
is only sensible on a machine where you wrote all of them.

## Settings reference

| Key | Built-in default | Values |
| --- | --- | --- |
| `theme` | `auto` | `dark`, `light`, `auto` |
| `transcript_max_chars` | `2000000` | Positive integer, retained text budget shared by resume and redraw; applies on next launch |
| `syntax_dark` | `gruvbox-dark` | A Pygments style for fenced code on the dark palette |
| `syntax_light` | `gruvbox-light` | A Pygments style for fenced code on the light palette |
| `autocompact` | `on` | `on`, `off` |
| `paced_scrollback` | `on` | `on`, `off` (roll settled blocks into scrollback a few rows per frame; see [the transcript](transcript.md#paced-scrollback)) |
| `code_mode` | `off` | `on`, `off` (batch read-only tools through a sandboxed `run_code`) |
| `tool_output_mode` | `spill` | `spill`, `truncate`, `off` |
| `tool_output_threshold` | `10000` | Positive integer, characters that trigger reduction |
| `tool_output_preview_chars` | `1000` | Positive integer, spill preview characters |
| `tool_output_max_chars` | `4000` | Positive integer, truncation budget (also spill-failure fallback) |
| `tool_output_strategy` | `head_tail` | `head`, `tail`, `head_tail` (truncation only) |
| `tool_output_retention_hours` | `0` | Whole number, spill retention; `0` keeps indefinitely |
| `btw_auto_open` | `on` | `on`, `off` (open the viewer when a [side answer](side-questions.md) is ready) |
| `meridian_managed` | `auto` | `auto`, `on`, `off` (use a running Meridian proxy or start a private one; see [Meridian](providers.md#which-meridian-pcode-uses)) |
| `profile` | `off` | `off`, `resources`, `cpu`, `memory` (capture each session's resource use; see [profiling](profiling.md)) |
| `repo_context_walk_up` | `on` | `on`, `off` (inherit ancestor instruction files) |
| `repo_context_nested` | `off` | `off`, `pointer`, `contents` (discover instructions on file-tool traversal) |
| `skill_commands` | `prefix` | `prefix`, `bare`, `both`, `off` (how discovered skills appear as slash commands) |
| `skill_dirs` | `~/.agents/skills:.agents/skills` | `:`-separated directories searched for skills; relative entries resolve against the workspace |
| `worktree` | `off` | `on`, `off` (start each new session in its own `.worktrees/` git worktree) |
| `worktree_exit` | `ask` | `ask`, `merge`, `keep` (what to do with unmerged commits when a session worktree is left) |
| `project_extensions` | `off` | `on`, `off` (`on` trusts every repository's `.pcode/extensions` and `worktree-setup`) |
| `trusted_projects` | `` | `:`-separated repository paths whose shipped code may run; the launch prompt appends here |
| `extension_dirs` | `` | `:`-separated extra directories searched for extensions, after the user one |
| `extensions_off` | `` | `,`-separated extension names that never load (`/extensions off NAME`) |
| `extensions_on` | `` | `,`-separated opt-in extension names to load (`/extensions on NAME`) |
| `effort` | `default` | `low`, `medium`, `high`, `xhigh`, `default` (OpenAI/Codex, Anthropic, Meridian); fallback for models `/effort` has not set |
| `model` | `null` (offline preview) | A model name, normally `provider:model` |

## Code highlighting styles

Fenced code keeps its own background, so each palette gets its own Pygments
style: `syntax_dark` applies whenever the resolved theme is dark, `syntax_light`
whenever it is light. `/syntax NAME` changes the style for the palette in use and
saves it as that palette's default; `/syntax` alone reports the current one. Tab
completion lists the styles, and an unknown name is rejected with the full list.
`/theme-preview` renders each of them on one line, marks the one in use, and
repeats the commands below, so a style can be chosen by eye rather than by name.

The completion menu and the prompt chrome (the chevron, plan rows, the frame,
`@file` references) are painted from the same style, so the screen matches the
code on it. A Pygments style only colors code, though, so any color it leaves
out or that would be unreadable falls back to the palette's own. The two are
judged against different backgrounds: the menu brings the style's own surface
with it, while chrome lands on the terminal's background, so a light style
chosen while the dark palette is active keeps its popup but leaves the chrome
on the palette. `/colors terminal` drops the style entirely and both return to
the palette.

These are the styles Pygments installs here; a Pygments style plugin package adds
to the list automatically.

Darker backgrounds: `coffee`, `dracula`, `fruity`, `github-dark`, `gruvbox-dark`,
`inkpot`, `lightbulb`, `material`, `monokai`, `native`, `night-owl`, `nord`,
`nord-darker`, `one-dark`, `paraiso-dark`, `rrt`, `solarized-dark`, `stata-dark`,
`vim`, `zenburn`.

Lighter backgrounds: `abap`, `algol`, `algol_nu`, `arduino`, `autumn`, `borland`,
`bw`, `colorful`, `default`, `emacs`, `friendly`, `friendly_grayscale`, `igor`,
`lilypond`, `lovelace`, `manni`, `murphy`, `paraiso-light`, `pastie`, `perldoc`,
`rainbow_dash`, `sas`, `solarized-light`, `staroffice`, `stata-light`, `tango`,
`trac`, `vs`, `xcode`.

Styles differ in how much they color: some leave plain identifiers and
punctuation at the default foreground, so a snippet that is mostly names can look
unhighlighted even though the lexer ran. Compare a few against your own terminal
background before settling on one.

`/colors terminal` ignores both settings and uses `ansi_dark` / `ansi_light`
instead, which follow the terminal's own sixteen colors. In that mode
`/theme-preview` lists the style names without samples: drawing them would need
the RGB colors that mode exists to avoid.

Outside the editor, `pcode config set syntax_dark NAME` and
`pcode config set syntax_light NAME` save the same two settings.

## Notes

Automatic compaction still requires a known context window; setting its global
preference does not validate a particular model or trigger a compaction. For custom
deployments, use `PCODE_CONTEXT_WINDOW` as described in
[context compaction](context.md#context-compaction). `/colors` / `--color-style`
remain session-only; MCP configuration and credentials are separate from these
non-secret defaults.

Writes are atomic and serialized across terminals. Unknown JSON keys are preserved;
invalid setting values fall back to built-in defaults. Normal startup tolerates a
malformed file, but config commands report it and refuse to overwrite it: use
`pcode config path` to find and repair it first. Invalid commands exit nonzero.

`PCODE_CONFIG_DIR` overrides the user config directory for preferences, extensions,
MCP configuration, worktree setup, and stored logins. Otherwise pcode uses
`$XDG_CONFIG_HOME/pcode`, defaulting to `~/.config/pcode`. Per-file overrides
(`PCODE_CREDENTIALS_FILE`, `PCODE_CODEX_CREDENTIALS_FILE`, `PCODE_MCP_CONFIG`)
take precedence.
