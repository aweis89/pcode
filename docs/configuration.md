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
**Most config edits affect the next launch, not the running conversation.** To change
an active setting and save its default immediately, use `/theme`, `/effort`,
`/model`, or `/autocompact` instead. The layout settings `attach_tasks`,
`tasks_max_height`, `tasks_min_rows`, `tasks_min_columns`, `task_style`, `tool_glyphs`, `tool_max_lines` and `tool_linger_seconds` apply immediately through `/config`. CLI overrides such as
`--theme` and `--model` do not rewrite global defaults, and resumed sessions retain
their own model.

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
`extensions_off`, `extensions_on`, `meridian_managed`, and `anthropic_auth` are
user-only: the project file cannot set them, and pcode says so at launch if it
tries. `model` and `subagent_models` can be set, but only choose among the
providers you are signed in to; `/subagents` names a list the repository set. The overlay is read from the
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

Every key works with `pcode config set KEY VALUE` and `/config set KEY VALUE`.

### Models and providers

| Key | Built-in default | Values |
| --- | --- | --- |
| `model` | `null` (offline preview) | A model name, normally `provider:model` |
| `effort` | `default` | `low`, `medium`, `high`, `xhigh`, `default` (OpenAI/Codex, Anthropic, Meridian); fallback for models `/effort` has not set |
| `model_providers` | `` | `,`-separated providers `/model` lists; empty shows every provider you're signed in to or have a key for |
| `anthropic_auth` | unset | `api-key`, `oauth` (which Anthropic credential `anthropic:` models use; set by `/login`, overridden by `PCODE_ANTHROPIC_AUTH`) |
| `meridian_managed` | `auto` | `auto`, `on`, `off` (use a running Meridian proxy or start a private one; see [Meridian](providers.md#which-meridian-pcode-uses)) |
| `claude_idle_processes` | `1` | Whole number of finished [`claude:`](providers.md#claude-code-provider) CLI processes each session keeps warm (about 120 MB each beyond the first); `0` stops each when its turn ends. None are kept under memory pressure |
| `claude_idle_minutes` | `10` | Positive integer, minutes a finished `claude:` CLI process is kept warm |
| `retry_attempts` | `3` | Whole number, automatic retries after a dropped connection; `0` disables. See [retries](sessions.md#retries-and-resend) |

### Editor and keys

| Key | Built-in default | Values |
| --- | --- | --- |
| `send_mode` | `steering` | `steering`, `queue`, `interrupt` (what Enter does while a turn runs; see [sending while the agent is working](commands.md#sending-while-the-agent-is-working)) |
| `editing_mode` | `emacs` | `emacs`, `vi` (prompt editor key bindings; see [vi editing](commands.md#optional-vi-editing)) |
| `key_prefix` | `ctrl+b` | A leader pressed before the action letter: Ctrl+B opens the current action menu. Other leaders include `ctrl+p`, `ctrl+space`, `f2` or `"ctrl+x ctrl+p"`; `ctrl` restores direct Ctrl+letter chords. F1 browses contextual help. See [shortcut prefix](commands.md#shortcut-prefix) |
| `popup_mouse` | `on` | `on`, `off` (popups capture clicks and the wheel; `off` keeps native text selection, Ctrl+B `q` flips it inside one popup, see [popup keys](commands.md#popup-keys)) |
| `btw_auto_open` | `on` | `on`, `off` (open the viewer when a [side answer](side-questions.md) is ready) |

### Scrollback and display

| Key | Built-in default | Values |
| --- | --- | --- |
| `theme` | `auto` | `dark`, `light`, `auto` |
| `syntax_dark` | `terminal` | `terminal` or a Pygments style, for the dark palette |
| `syntax_light` | `terminal` | `terminal` or a Pygments style, for the light palette |
| `spinner` | `dots` | The status row's animation while the model works. A Rich spinner name, e.g. `dots`, `line`, `point` (`python -m rich.spinner` previews them; `/config set spinner` lists the ones that fit; applies on next launch) |
| `tool_spinner` | `arc` | The same, for a running tool call's row and the terminal's own waits, so they look different from the model's |
| `show_thinking` | `status-line` | `off`, `status-line`, `scrollback` (where the model's readable reasoning shows; `/show-thinking`) |
| `thinking_max_lines` | `10` | Max rows of thinking above the status row in `status-line` mode, or a share of the screen (`0.2`). A row count is also capped at a quarter of the pane, so a short pane gets fewer. Applies on next launch |
| `show_edits` | `on` | `on`, `off` (show a diff of each file edit; `/show-edits`) |
| `live_edits` | `off` | `on`, `off` (preview an edit or `run_code` snippet at the bottom while the model writes it) |
| `diff_renderer` | `delta` | `delta`, `rich` (draw diffs in scrollback and `/diffs` with [delta](https://dandavison.github.io/delta/) when it's installed, falling back to Rich; see [diffs with delta](transcript.md#diffs-with-delta)) |
| `delta_args` | `` | delta's arguments, quoted as in a shell, such as `--line-numbers`; the only delta configuration pcode reads (git config is ignored), and they override pcode's own choices |
| `diff_dedent` | `on` | `on`, `off` (strip the indentation every line of a diff hunk shares, so an edit deep in a nested block starts at the left edge; with delta or Rich; see [diffs with delta](transcript.md#diffs-with-delta)) |
| `diff_layout` | `unified` | `unified`, `side-by-side`, `auto` (delta's layout; `auto` goes side by side at 180 columns or wider) |
| `show_commands` | `off` | `on`, `off` (mirror each shell command and its output into scrollback; Ctrl+B `g` or `/show-commands`) |
| `group_tools` | `on` | `on`, `off` (fold each run of tool calls into one line; `/group-tools`, see [grouping tool calls](transcript.md#grouping-tool-calls)) |
| `command_scrollback_lines` | `20` | Positive integer, lines of each command's output mirrored into scrollback |
| `command_preview_lines` | `10` | Rows (`10`) or a share of the screen (`0.25`) for the live preview of a running command |
| `tool_error_scrollback` | `off` | `on`, `off` (keep a failed tool call's full diagnostic in scrollback instead of one line) |
| `error_scrollback_lines` | `20` | Positive integer, lines of an error notice kept in scrollback before it is clipped |
| `show_tasks` | `on` | `on`, `off` (show the Tasks/Tools widget; Ctrl+B `o` or `/show-tasks`) |
| `autohide_tasks` | `off` | `on`, `off` (hide the Tasks/Tools widget when a turn ends; `/autohide-tasks`) |
| `tasks_min_rows` | `30` | Hide the Tasks/Tools widget in a pane shorter than this, such as a stacked split; `0` never hides. Ctrl+B `o` overrides it until the pane crosses the threshold again |
| `tasks_min_columns` | `100` | Hide the Tasks/Tools widget in a pane narrower than this, such as a side-by-side split; `0` never hides. Ctrl+B `o` overrides it the same way |
| `show_hints` | `on` | `on`, `off` (show one compact help indicator at the prompt instead of shortcut hints beside individual controls) |
| `attach_tasks` | `on` | `on`, `off` (draw tasks inside the editor box; `/config` applies immediately) |
| `tool_glyphs` | `auto` | `on` leads the tool row above the status row with a symbol for the call (`$`, `⌕`, `✎`, `⎘`, `⧖`), `off` drops them and spells out the verb once the call settles (shell calls keep `$`), for fonts that draw those symbols badly. `auto` is on except on the Linux console (`/config` applies immediately) |
| `tool_max_lines` | `3` | Max tool calls listed above the status row, newest last; `1` shows only the call the status row is on. A short pane gets fewer, sharing a quarter of its height with the thinking rows (`/config` applies immediately) |
| `tool_linger_seconds` | `10` | Seconds a finished tool call stays listed above the status row, so a long wait on the model doesn't keep showing stale calls; `0` keeps it until newer calls push it out (`/config` applies immediately) |
| `task_style` | `status` | `status` (shade task text by status), `icons` (one text weight, coloured icons only; `/config` applies immediately) |
| `tasks_max_height` | unset | Rows (`20`) or a share of the screen (`0.5`) for the Tasks/Tools widget and editor together; unset keeps the widget to 10 rows or half the screen |
| `paced_scrollback` | `typed` | `typed`, `rows`, `off` (type settled prose out, or roll blocks in a row per frame; see [the transcript](transcript.md#paced-scrollback)) |
| `regenerate_on_resize` | `on` | `on`, `off` (rebuild scrollback at the new size after a resize; applies on next launch) |
| `transcript_max_chars` | `2000000` | Positive integer, retained text budget shared by resume and redraw; applies on next launch |
| `cache_notices` | `on` | `on`, `off` (footer note, saved with the session, when a request reuses less of the prompt cache; see [prompt cache notices](context.md#prompt-cache-notices)) |
| `terminal_progress` | `auto` | `auto`, `on`, `off` (the terminal's [tab progress bar](#tab-progress-bar) while a turn runs; OSC 9;4) |
| `desktop_notifications` | `on` | `on`, `off` (desktop notification when a background session finishes; OSC 9) |
| `terminal_title` | `on` | `on`, `off` (set the terminal [tab title](#tab-title) to the session's name; OSC 0) |
| `session_naming` | `on` | `on`, `off` (ask the session's own model for a [title](sessions.md#session-titles) after the first turn) |

### Tools and sub-agents

| Key | Built-in default | Values |
| --- | --- | --- |
| `web_search` | `auto` | `auto`, `local`, `off` (provider-native web search when available, pcode's own, or none; see [web search](tools.md#web-search)) |
| `code_mode` | `off` | `on`, `off` (batch read-only tools through a sandboxed `run_code`) |
| `job_wake` | `on` | `on`, `off` (start a turn when a job the model backgrounded finishes while idle; see [shell jobs](tools.md#shell-jobs)) |
| `tool_retries` | `3` | Whole number, corrections the model gets per turn when a tool call has invalid arguments |
| `strict_tools` | `on` | `on`, `off` (constrain `edit_file` arguments with Anthropic strict tool use) |
| `subagent_models` | `` | `,`-separated models `delegate_task` lists for running a sub-agent; others can still be named per delegation ([`/subagents`](tools.md#sub-agents-on-other-models)); empty runs every sub-agent on the session's model; `/reload` to apply |
| `worker_concurrency` | `0` | `0` means unlimited; a positive integer caps concurrent built-in workers per session; `/reload` to apply |
| `tool_output_mode` | `spill` | `spill`, `truncate`, `off` |
| `tool_output_threshold` | `10000` | Positive integer, characters that trigger reduction |
| `tool_output_preview_chars` | `1000` | Positive integer, spill preview characters |
| `tool_output_max_chars` | `4000` | Positive integer, truncation budget (also spill-failure fallback) |
| `tool_output_strategy` | `head_tail` | `head`, `tail`, `head_tail` (truncation only) |
| `tool_output_retention_hours` | `0` | Whole number, spill retention; `0` keeps indefinitely |

### Context

| Key | Built-in default | Values |
| --- | --- | --- |
| `autocompact` | `on` | `on`, `off` |
| `autocompact_tokens` | unset | Tokens such as `200000` or `200k` (minimum 50k) at which to compact automatically; unset uses about 90% of the window |

### Sessions and worktrees

| Key | Built-in default | Values |
| --- | --- | --- |
| `session_host` | `on` | `on`, `off` (run sessions in a [background host](sessions.md#background-sessions) that outlives the terminal; `--host`/`--no-host` override it once) |
| `session_host_idle_minutes` | `0` | whole minutes a [background session](sessions.md#idle-hosts-stop) may sit idle with no terminal before its host stops; `0` stops it 15 seconds after it goes idle, `off` never |
| `worktree` | `off` | `on`, `off` (start new sessions in `.worktrees/` git worktrees; does not enable worker isolation on its own) |
| `worktree_exit` | `ask` | `ask`, `merge`, `keep` (what to do with unmerged commits when a session worktree is left) |
| `worker_isolation` | `off` | `on`, `off` (opt in to isolated built-in worker tasks; also requires effective `worktree=on`, not just the CLI launch override; checked at each delegation) |

### Email remote control

Settings for [`pcode --email-listen`](email.md). All of them are user-only: a repository's `.pcode/preferences.json` cannot set them.

| Key | Built-in default | Values |
| --- | --- | --- |
| `email_owner` | unset | the Gmail address allowed to control pcode by email; set by `pcode --email-setup` |
| `email_mcp` | `off` | `on`, `off` (start default MCP servers in email-started sessions; they run outside the sandbox) |
| `email_turn_minutes` | `30` | wall-clock minutes per email-started turn; `0` is no limit |
| `email_turn_requests` | `100` | model requests per email-started turn, sub-agents included; `0` is no limit |
| `email_turn_tool_calls` | `100` | tool calls per email-started turn, sub-agents included; `0` is no limit |
| `email_concurrent_sessions` | `2` | email sessions that may be working at once |
| `email_max_sessions` | `20` | sessions one `--email-listen` may start |
| `email_session_inputs` | `20` | emails that may wait in one session |
| `email_max_inputs` | `100` | emails that may wait across every session |

### Repository instructions, skills and extensions

| Key | Built-in default | Values |
| --- | --- | --- |
| `repo_context_walk_up` | `on` | `on`, `off` (inherit ancestor instruction files) |
| `repo_context_nested` | `off` | `off`, `pointer`, `contents` (discover instructions on file-tool traversal) |
| `skill_commands` | `prefix` | `prefix`, `bare`, `both`, `off` (how discovered skills appear as slash commands) |
| `skill_dirs` | `~/.agents/skills:.agents/skills` | `:`-separated directories searched for skills; relative entries resolve against the workspace |
| `project_extensions` | `off` | `on`, `off` (`on` trusts every repository's `.pcode/extensions` and `worktree-setup`) |
| `trusted_projects` | `` | `:`-separated repository paths whose shipped code may run; the launch prompt appends here |
| `extension_dirs` | `` | `:`-separated extra directories searched for extensions, after the user one |
| `extensions_off` | `` | `,`-separated extension names that never load (`/extensions off NAME`) |
| `extensions_on` | `` | `,`-separated opt-in extension names to load (`/extensions on NAME`) |

### Diagnostics

| Key | Built-in default | Values |
| --- | --- | --- |
| `debug` | `off` | `on`, `off` (also write request fingerprints to disk with each cache notice) |
| `profile` | `off` | `off`, `resources`, `cpu`, `memory` (capture each session's resource use; see [profiling](https://github.com/aweis89/pcode/blob/master/dev/profiling.md)) |
| `stall_log` | `on` | `on`, `off` (when the editor stops responding for roughly 150 ms or more, append what was running to `~/.local/state/pcode/stalls.jsonl`) |

## Tab title

Once a session has a name, from `/rename` or the
[title its model gave it](sessions.md#session-titles), pcode puts it in the
terminal's tab title, so a row of tabs reads as a list of tasks. It follows
`/rename`, `/new` and `/switch`. A session with no name yet leaves the title to
your shell, and on exit pcode puts back the title it found, in terminals that
keep a title stack (xterm, iTerm2, kitty, Ghostty and VTE terminals such as
GNOME Terminal, among others). Elsewhere the title stays until your shell's
prompt sets its own.

Inside tmux the name goes to the pane title. tmux shows it in the outer tab
only with `set -g set-titles on`, whose default `set-titles-string` includes the
pane title. `pcode config set terminal_title off` leaves the title alone.

## Tab progress bar

While a turn runs, pcode reports progress to the terminal itself (OSC 9;4),
which draws it outside the screen: Ghostty and kitty as a thin bar along the
top of the split, iTerm2 in the pane's top margin, Windows Terminal in the
tab, WezTerm wherever its
Lua config puts it. A busy tab is visible from the others.

| Bar | Means |
| --- | --- |
| Moving, no fill | A turn is running |
| Filling | A turn is running with a plan; the fill is how much of the plan is done |
| Paused (orange in Ghostty) | A failed provider request is being retried |
| Error (red in Ghostty) | The last turn failed; any key in that terminal, or the next turn, clears it |

The fill is weighted by step size. The model can mark a step S, M or L, so
one big implementation step counts for more than a quick check beside it.
Unmarked steps count as M. The running step counts as half done, so the bar
moves as soon as work starts, and cancelled steps drop out of the total.

`--print` shows the bar too, from launch until it exits (with `--attach`, while
the host works on the message or on the turns queued ahead of it), and takes
it down on exit. It goes to stderr, or to stdout when only that is a terminal,
so a reply piped elsewhere still leaves the bar on the terminal. When neither
is a terminal, nothing is sent.

The protocol carries only a state and a percentage, so the colours are the
terminal's: Ghostty uses the macOS accent colour for a running bar, kitty uses
its `scrollbar_*` colours. Hide it on the terminal's side with Ghostty's
`progress-style = false` or kitty's `progress_bar hidden`.

iTerm2 3.7 or newer can restyle it per profile, under **Settings > Profiles >
Session**: **Progress bar height** (in points, 2 by default) and **Progress
bar color scheme** (Default, Rainbow, or a single colour). In a Dynamic Profile the
keys are `"Progress Bar Height"` and `"Progress Bar Color Scheme"`, and both
need `"Enable Progress Bars"`. iTerm2 3.6.6 draws the bar but has neither
setting.

`auto` sends it only to terminals whose environment variables say they draw
it: Ghostty, WezTerm, iTerm2 3.6.6 or newer, Windows Terminal, ConEmu, VS
Code, Warp, mintty, VTE 0.79 terminals (GNOME Terminal, Ptyxis) and Konsole
26.04. Older iTerm2 and kitty before 0.38 read the sequence as a desktop
notification, which is why an unknown terminal gets nothing. kitty is among
them because it reports no version; on kitty 0.47 or newer, set
`terminal_progress on`. `on` sends it to any terminal, and terminals that
do not know it ignore it.

Inside tmux the terminal is judged by what the tmux server's environment
inherited from the terminal it was started in (tmux replaces `TERM` and
`TERM_PROGRAM`), and each report is sent twice: once through passthrough and
once raw. Passthrough needs `allow-passthrough on` and is the reliable way,
since Ghostty drops a report that is not refreshed within about 15 seconds and
pcode refreshes it every few. Without it, tmux 3.7 or newer forwards the
active pane's bar itself, but only when it changes, so a long turn's bar can
fade in Ghostty; older tmux drops it. Two pcode panes side by side share the
window's one bar, which shows whichever reported last.

```tmux
set -g allow-passthrough on
```

The setting is read when pcode starts.

## Code highlighting styles

Each palette gets its own setting: `syntax_dark` applies whenever the resolved
theme is dark, `syntax_light` whenever it is light. `/syntax NAME` changes the
setting for the palette in use and saves it as that palette's default; `/syntax`
alone reports the current one. Tab completion lists the choices, and an unknown
name is rejected with the full list.

Both default to `terminal`, which is not a Pygments style: it hands every color
to the terminal's own ANSI palette. Scrollback uses named colors, fenced code
uses `ansi_dark` / `ansi_light` with no painted background, and the prompt, task
rows and completion popup use ANSI names too (the selected popup row is
reversed). pcode then looks right in whatever scheme the terminal runs.

`/theme-preview` draws a sample of every style on one line, marks the one in
use, and repeats the commands below, so a style can be chosen by eye rather than
by name. The `terminal` row is drawn with the palette's ANSI style.

Any other value is a Pygments style. The completion menu and the prompt chrome
(the chevron, plan rows, the frame, `@file` references) are painted from it, so the screen matches the
code on it. A Pygments style only colors code, though, so any color it leaves
out or that would be unreadable falls back to the palette's own. The two are
judged against different backgrounds: the menu brings the style's own surface
with it, while chrome lands on the terminal's background, so a light style
chosen while the dark palette is active keeps its popup but leaves the chrome
on the palette. Picking any Pygments style also switches scrollback headings,
links, quotes and tables from ANSI names to the palette's own colors.

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

Outside the editor, `pcode config set syntax_dark NAME` and
`pcode config set syntax_light NAME` save the same two settings.

## Notes

Automatic compaction still requires a known context window; setting its global
preference does not validate a particular model or trigger a compaction. For custom
deployments, use `PCODE_CONTEXT_WINDOW` as described in
[context compaction](context.md#context-compaction). MCP configuration and
credentials are separate from these non-secret defaults.

Writes are atomic and serialized across terminals. Unknown JSON keys are preserved;
invalid setting values fall back to built-in defaults. Normal startup tolerates a
malformed file, but config commands report it and refuse to overwrite it: use
`pcode config path` to find and repair it first. Invalid commands exit nonzero.

`PCODE_CONFIG_DIR` overrides the user config directory for preferences, extensions,
MCP configuration, worktree setup, and stored logins. Otherwise pcode uses
`$XDG_CONFIG_HOME/pcode`, defaulting to `~/.config/pcode`. Per-file overrides
(`PCODE_CREDENTIALS_FILE`, `PCODE_CODEX_CREDENTIALS_FILE`, `PCODE_MCP_CONFIG`)
take precedence.
