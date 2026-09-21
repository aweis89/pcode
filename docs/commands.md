# Commands and keys

## Offline preview and commands

```sh
uv run pcode                 # no model, canned replies only
uv run pcode --theme-preview # print a sample and the style gallery, then exit
uv run pcode --theme light   # light input palette
uv run pcode --theme auto    # detect terminal background at startup
```

Type `/` to open the command menu, then narrow it by typing. Use Tab or the
arrow keys to choose. Enter accepts a selected completion; another Enter runs it.

Type `@` anywhere in a prompt to reference a workspace file. The menu matches
any part of the path, so `@ui.py` finds `src/pcode/ui.py`; files whose name
matches come first, and each row shows the file's size. Accepting a match
replaces `@…` with the workspace-relative path, `./src/pcode/ui.py`, which is
the form the model's file tools take, so it can read or search the file without
guessing where it lives (a name containing spaces is quoted). References are
underlined in the editor. The candidate list comes from `git ls-files` (tracked
plus untracked, honoring `.gitignore`), or a directory walk that skips hidden
and build directories outside a Git checkout, and is refreshed at most every ten
seconds.

The path is all that is sent: pcode never reads a referenced file for you, so
the model decides whether reading it is worth a call. The menu shows each
candidate's size so that cost is visible before you pick.

- `/theme-preview`: fictional Markdown, code, diff, table, and tool summaries,
  followed by a gallery of every installed Pygments style with the commands that
  select one. Never calls the model, even in live mode, and does not enter its
  conversation history. `--theme-preview` (formerly `--demo`, still accepted)
  prints the same thing without a terminal.
- `/theme light`, `/theme dark`, or `/theme auto`: change the input and future output palette.
  Auto uses the terminal background detected at startup with an OSC 11 query,
  falling back to `COLORFGBG`, then dark when unavailable (including redirected
  output). Restart pcode after changing your terminal background. Save auto mode
  with `/theme auto` or `pcode config set theme auto`; the built-in default remains dark.
  `/theme` alone toggles. By default, Rich headings, links, quotes, inline code,
  and tables follow this palette; fenced code uses the palette's own Pygments
  style, `gruvbox-dark` or `gruvbox-light`. Normal body text and the overall
  background remain terminal-native.
- `/syntax NAME`: change the Pygments style for fenced code, the completion menu
  and the prompt chrome on the active palette and save it as that palette's
  default; `/syntax` alone reports the current
  style. See [Code highlighting styles](configuration.md#code-highlighting-styles) for the list;
  `/theme-preview` renders every style, marking the one in use.
- `/colors terminal`: opt into terminal-defined ANSI colors with unpainted code
  backgrounds and `ansi_dark` / `ansi_light` syntax. `/colors palette` restores
  the default coordinated palette; `/colors` shows the current selection.
  This affects Rich output, not the input/completion palette. You can also start
  with `--color-style terminal` (default: `--color-style palette`). Run
  `/theme-preview` after switching to compare headings, links, quotes, tables, Python, and diffs.
  Existing scrollback is not repainted.
- Session, conversation-tree, model, and tool popups share terminal-default
  backgrounds and text, with reverse-video selection highlights. They follow your
  terminal background automatically, independently of `/theme` and `/colors`.
- `/help` (or `/commands`): grouped command list and keyboard shortcuts.
- `/login`: sign in to Anthropic in a browser; `/logout` removes pcode's stored login. Both require an idle conversation.
- `/model`: searchable model picker for configured providers (keeps the conversation;
  applies from the next request when chosen mid-run).
- `/tools`: scrollable tool-call inspector for the current conversation, including resumed calls.
- `/tools failed`: open the same inspector filtered to failures.
- `/diffs`: browse this conversation's file diffs in a full-screen popup.
- `/links`: pick any URL mentioned in this conversation (your prompts or the
  assistant's replies, newest first) and open it in the default browser via
  `open` (macOS), `xdg-open` (Linux), or the shell association (Windows). Useful
  when the terminal or an older tmux does not make rendered links clickable.
- `/status`: current model, workspace, session storage path, completed turns, token usage,
  and a breakdown of the prompt overhead the model is re-sent every request — see
  [Where the fixed prompt goes](context.md#where-the-fixed-prompt-goes). Opens a popup in the
  interactive editor; prints inline when there is no editor.
- `/resend`: retry from the last checkpoint without a new message; shows the previous prompt and spinner.
- `/compact [focus]`: summarize older context with the current model; keep recent history.
- `/autocompact on|off`: opt into automatic LLM compaction (saved user preference; default off).
- `/new`: start a new saved conversation; clears the screen and retained scrollback, keeps input history.
- `/resume`: browse and search saved conversations by their prompts; resume one in place.
- `/tree`: [browse and fork the conversation](conversation-tree.md); select a user prompt to
  edit it, or an assistant response to continue from there. Existing branches are kept.
- `/skill:NAME [text]`: run a discovered skill; see
  [Skills as slash commands](workspace.md#skills-as-slash-commands) for naming and configuration.
- `/quit` (alias `/exit`): exit.

## Keys and layout

| Key | Action |
| --- | --- |
| Enter | Send using the active send mode, or accept a selected completion |
| Ctrl+S | Cycle steering → queue → interrupt (saves the default) |
| ↓ | Newline when on the last line with nothing to complete or recall (works in vi insert mode) |
| Ctrl+J / Shift+Enter | Newline; see [Newlines in tmux](#newlines-in-tmux) if neither reaches pcode |
| Alt+Enter | Newline in Emacs mode only (Esc followed by Enter also works) |
| Tab / arrows | Browse completion; arrows also navigate input/history |
| Ctrl+L | Choose a model (keeps the conversation; applies from the next request) |
| Ctrl+O | Show/hide the Tasks/Tools widget (saves the default) |
| Ctrl+R | Search this process's input history |
| Ctrl+G | Mirror commands and their output to scrollback (saves the default) |
| Ctrl+C | Discard input; cancels the running turn only when the prompt is empty |
| Ctrl+D | Exit on empty idle input; cancel during generation |

Press **Ctrl+O** or use `/show-tasks [on|off]` to hide or show the Tasks/Tools
widget without stopping work or clearing task/tool history. The current prompt
and queue remain visible. Visibility is saved across launches (default: on);
use `pcode config set show_tasks off` to set the default from the shell.

The widget also hides itself as soon as the model finishes a turn, keeping the
idle prompt compact, and returns on the next turn. Turn that off with
`/autohide-tasks off` (or `pcode config set autohide_tasks off`); Ctrl+O brings
the widget back immediately after an auto-hide.
Ctrl+O replaces the editor’s insert-newline binding; Ctrl+J still inserts a newline.

**Setting acknowledgements are transient.** Toggles and display settings
(`/show-thinking`, `/show-tasks`, `/show-edits`, `/show-commands`,
`/autohide-tasks`, `/autocompact`, `/theme`, `/colors`, `/syntax`, `/effort`)
answer on a line directly above the spinner, just over the editor, and clear
themselves after five seconds. They never enter terminal scrollback, so
flipping a display option repeatedly does not litter the transcript, and a
transcript rebuild (`/redraw`, resize replay) neither preserves nor duplicates
them. Long acknowledgements wrap to the pane and are capped at six rows.
Everything else a command reports — `/status`, `/mcp`, `/help`, login flows,
session and model changes, warnings, and errors — still goes to scrollback.
Without a live panel (redirected output, `--print`) an acknowledgement falls
back to a printed notice.

## Optional vi editing

The prompt uses Emacs-style editing by default. Enable vi bindings for subsequent
launches with:

```sh
pcode config set editing_mode vi
```

You can also run `/config set editing_mode vi` in a session, then restart pcode.
The editor starts in insert mode; press Escape for normal mode and `i` or `a` to
resume inserting. Use `o` / `O` in normal mode to open a line below / above
and enter insert mode. Standard vi motions and editing commands are available.
Enter still submits (or accepts a selected completion), and Ctrl+J inserts a
newline. Escape is prioritized in vi mode: Escape followed by Enter submits,
rather than inserting a newline. Alt+Enter is therefore a newline shortcut only
in Emacs mode. Other pcode application shortcuts retain their existing behavior.

**Shift+Enter inserts a newline** when your terminal sends a distinct CSI-u or
xterm modifyOtherKeys sequence. Ctrl+J supports both the traditional LF byte and
those extended encodings. If your terminal sends ordinary Enter for Shift+Enter,
pcode cannot distinguish them: configure Shift+Enter to send Ctrl+J (the single
LF byte, hex `0a`, often written `\x0a`). This is a terminal key mapping, not a
pcode setting; verify it inside tmux too if you use it. The ↓ key inserts a
newline whenever it would otherwise do nothing (last line, no completion menu,
not browsing older history), so it works even where no chord gets through.

## Newlines in tmux

Inside tmux, Shift+Enter arriving as a plain Enter is almost always tmux, not
the terminal. Two things have to be true, and `extended-keys on` alone gives you
neither:

- tmux only asks the outer terminal for modified keys when its terminfo
  advertises `extkeys`; Ghostty's and kitty's do not, so declare it.
- `extended-keys on` forwards those keys only to apps that opted into the
  protocol themselves. pcode (prompt_toolkit) does not, so use `always`.

```tmux
set -as terminal-features ',xterm-ghostty:extkeys'
set -g extended-keys always
set -g extended-keys-format csi-u
```

Reload, then detach and reattach: `#{client_termfeatures}` is computed when a
client connects. Check with `cat -v`: Shift+Enter should print `^[[13;2u`. If
Ctrl+J prints `^[[B` instead, a remapper (Karabiner, a Ghostty `keybind`) is
turning it into ↓ before tmux sees it; ↓ still inserts a newline on the last
line, so that is usually fine.

Vi mode uses a 100 ms terminal escape-sequence timeout and an eager Escape binding.
This avoids waiting for an Alt-key chord before entering normal mode; particularly
slow or fragmented terminal connections may need a longer timeout in future.

Restore the default with `pcode config set editing_mode emacs` or
`pcode config unset editing_mode`.

The input is bottom-aligned from startup, with one editable line plus its border.
It expands upward for wrapped text or explicit newlines, and shrinks when text is
removed. Completion appears above the frame. Very long input scrolls within the
available pane height. Multiline bracketed paste works; mouse capture is off.

Wrapping is word aware: a word that would straddle the right edge moves to the
next row whole, instead of being cut in half. The buffer text is unchanged — the
padding is display only, so editing positions, selection, and what gets sent are
all unaffected. A single word wider than the pane still has to be split.

Tasks and recent tool activity share one compact, headerless widget above the
editor. Task rows show status icons and keep the active item visible. Up to five
recent tool calls appear as indented subitems immediately below the active task;
this rolling view follows the currently active item, rather than recording
historical task ownership. When there is no active task (including no plan),
tools appear as unparented rows in the same widget instead of beneath a completed
or pending task. There is no separate Tools panel or Tasks heading.

The shared height budget shrinks in small panes, preserving the active task and
the newest tool calls. Empty tool slots are not reserved, and an empty widget is
hidden. Each call updates in place from running to success/failure; cancellation
marks unfinished calls as interrupted, not undone. Rows show a status icon, tool
name, elapsed time, and truncated path/command summary, without call numbers or
run counters. The latest ten calls remain in bounded internal history, and saved
session resume restores this history independently of conversation replay.
Task additions and status changes preview as soon as their fields arrive in the
model's streamed tool arguments, without waiting for the full response or tool
execution. These previews are display-only: tool results reconcile them to the
confirmed plan, and cancellation or failure discards any unconfirmed preview.
Only confirmed plans are saved. Successful planning operations update the task
rows without duplicate tool rows; failed planning operations remain visible as
failed calls. Plans and tools persist across turns and saved-session resumes;
`/new` clears both.
Routine tool summaries no longer enter conversation scrollback. `/tools` opens a
read-only alternate-screen inspector, separate from this ten-call activity panel.
It retains all live conversation calls, including successful planning operations.
The non-interactive `--theme-preview` sample still prints its fictional tool
summaries.

## Status line

The line below the editor shows the workspace/branch, full `provider:model`
identifier, reasoning effort, and activity. Live models also show context, for
example `ctx: 12.5k/200k` (tokens used / effective working window).

Used context is the **latest completed request's input tokens**, including cached
input, not cumulative session usage. It updates after a turn and follows the
selected conversation history when resuming or navigating branches. It does not
include unsent drafts, subsequent tool results, or a response still streaming.
The working window is shared with compaction. Pcode first uses metadata from the
actual serving provider: Codex's authenticated models endpoint or Anthropic's
Models API. Otherwise it uses an exact provider/model match from
[Models.dev](https://models.dev/), where the serving endpoint matches. Codex never
borrows ordinary OpenAI API limits, and custom proxies or Anthropic subscription
OAuth do not silently inherit direct-API limits. Catalog values remain advisory.
When separate input and combined-context ceilings are available, the smaller
ceiling is used; output capacity is retained separately, not added to input.

Metadata refreshes happen outside rendering, on startup, model selection, and
before requests when stale. Public metadata is cached for 24 hours in
`$XDG_CACHE_HOME/pcode/model-context-v1.json` (default `~/.cache/pcode/`). Native
metadata is cached for one hour **in memory per model instance**, not across
accounts or processes. Fetches have a three-second deadline and failures retain
last-known values with a one-minute retry backoff. No pricing is loaded into the
context cache or displayed. Unknown limits show `?`; a first run offline can thus
show `?`, and Codex needs a successful native lookup or an explicit override.
Used context shows `0` when the conversation is empty or usage has not been
reported yet.
Long paths shrink first; narrow terminals may truncate trailing context details.
`/status` continues to show cumulative session input/output usage.

## Sending while the agent is working

Enter uses the saved `send_mode` (default: `steering`). **Ctrl+S** cycles
`steering` → `queue` → `interrupt` and saves the selection; the status bar shows
which mode is active directly under the editor. Mode and working status take
priority over model and path metadata in narrow panes. Existing queued messages
keep their submission mode.

- **steering**: deliver input at the next model request, after active tools finish.
  Pending input is labeled “Steering (next model request)”; once delivered, it
  replaces the active prompt in the task bar. If the turn finishes before then,
  send it as a follow-up turn.
- **queue**: wait for the current turn to finish, then start a follow-up turn.
- **interrupt**: cancel the current turn, discard pending messages, and send the
  new message after cancellation cleanup completes.

Set the default with `pcode config set send_mode steering` (or `queue` / `interrupt`).
`/config set send_mode queue` changes the default for the next launch; Ctrl+S
changes it immediately. Idle input starts a normal turn in every mode. Slash
commands retain their existing behavior, and Ctrl+D (or Ctrl+C on an empty
prompt) still cancels and clears pending messages.

## Running a command yourself: `!command`

A message starting with `!` runs the rest as a shell command in the agent's
working directory and environment, without asking the model anything:

```text
❯ !make test
```

Output streams into the live command panel while it runs and is mirrored to
scrollback when it ends (the last `command_scrollback_lines` rows). Ctrl+C kills
the command and its process group. There is no timeout.

The model hears about it with your next message, as if it had called the
`shell` tool itself: the request carries a `shell` tool call for that command
followed by its output (and `[exit code: N]` when non-zero). The result goes
through the same `tool_output_*` reduction as real tool results, so a long test
log over `tool_output_threshold` characters is spilled to a `read_tool_result`
handle with a `tool_output_preview_chars` preview, and the model reads only
the slices it needs. A `!command` typed while a turn is running waits its turn
in the queue, whatever the send mode; nothing is sent to the model until you
send a message, so `!make test` followed by `why did that fail?` is the usual
shape.

## Edit diff browser

`/diffs` opens a full-screen popup showing this conversation's completed file
edits, using the same diff colors as scrollback. The diff fills most of the
screen; a small file selector sits at the bottom. Keys are listed in the header:

- Up/Down in the file list selects a file, newest change first.
- PageUp/PageDown scroll the diff without leaving the file list.
- Tab/Shift+Tab move focus; arrows and Ctrl+Home/Ctrl+End scroll the focused diff.
- Escape, Ctrl+C, or Ctrl+D closes the popup and restores the editor draft.

Saved sessions read their changes back from the journal on the active branch, so
resumed and branched conversations show the diffs that belong to them. Redaction
and size limits are the same as the scrollback blocks; nothing is re-read from
disk and no edit is re-applied.

## Tool-call inspector

Use `/tools` or `/tools failed`, including during an active turn.
The inspector shows a snapshot of the calls available when opened; reopen it to
see newer results. The model keeps running while the inspector is open, and
terminal output is buffered until it closes. Inspection never reruns a tool.

- Calls are newest first. Use arrows to select and Tab/Shift+Tab to move between
  the call list, detail pane, and search field.
- In the call list, **f** toggles failures, **t** cycles tool-name filters, and
  **/** focuses search. Search matches tool names/statuses and command/summary
  previews, not the complete output payload. Ctrl+F focuses search from any pane.
- In details, use arrows to scroll by line, PageUp/PageDown by page, or Ctrl+U/Ctrl+D
  by half a page. The session browser shares these content-pane controls.
- Mouse clicks and wheel scrolling work in the popups. In tmux, enable mouse
  forwarding with `tmux set -g mouse on` (or `set -g mouse on` in `~/.tmux.conf`).
- Escape or Ctrl+C closes only the inspector and restores the editor draft.
  Ctrl+D also closes it when the call list or search field has focus.
- Wide terminals show calls and details side by side; narrow terminals stack them.

Details include the call/run IDs, timestamp and duration when captured, structured
arguments, framework outcome, and returned output/error. Nonzero command exits,
timeouts, and tool retries are failures; interruption and unknown results remain
distinct. Background launch/check/stop calls show their process ID and related
calls when available. A successful launch is not proof that the process finished
successfully.

Saved inspection data is a redacted display projection in `transcript.jsonl`, not
an execution or recovery log. It survives resume and failures in later model
requests. Metadata is indexed incrementally; result payloads are read on selection.
Arguments and results are each capped at 128 Ki characters, with explicit truncation
markers. Tool-side truncation is preserved, not recoverable by the inspector.
Unsaved conversations retain at most 8 MiB of payload text; older evicted details
are labeled while call metadata remains. `/new` resets the inspector history.
Older saved calls remain browsable but show a missing-details label where their
journal did not capture arguments/results. Preview mode shows its recent fixtures.
Redaction is best-effort, not a guarantee that arbitrary sensitive content is removed.

The editor avoids full-screen erase sequences that terminals such as tmux can
copy into scrollback, leaving duplicate borders after a resize. Repeated
horizontal and vertical shrink/grow cycles, including multiline drafts, are
covered by real-tmux tests with cursor-position reports enabled.

Live responses stream as literal text into normal terminal scrollback, including
Markdown markers. Complete lines are printed once; only the unfinished display
line is live. Rich's `Text.wrap()` wraps at spaces using the current terminal width, keeping
words together across streamed chunks. The separating space becomes a newline;
explicit newlines and indentation are retained. Tokens longer than the available
width must still split. The live tail stays small. Finishing a message flushes the
tail without replacing the response
with rendered Markdown. Tool summaries live inside the task widget.
`/theme-preview` and restored session messages still use Rich Markdown.

The editor remains usable throughout generation, including multiline input,
history, slash completion, and `@` file references. Enter sends using the active mode (steering by
default) and clears the editor for another draft; the toolbar shows the mode and
pending message count. Steering messages join the next model request; queue-mode
messages run in order after the current turn finishes. Ctrl+S cycles send modes. Slash commands use a separate async
handler, so help, inspection, theme, context, and effort controls remain available
while the model works. `/model` also opens while working and applies from the next
request. `/new`, `/resume`, `/login`, and `/logout` require an idle conversation: cancel
or wait, then retry. `/quit` (or `/exit`) cancels the active run and waits for its
cleanup before exiting. Ctrl+D cancels the current turn, clears queued messages,
and preserves the unsubmitted draft and cursor. Ctrl+C discards the draft first,
so cancelling with it takes a second press when the prompt has text. A failed turn also
clears queued messages rather than automatically running more requests. Commands
are not discarded with that queue. The queue is in memory only, not saved until
submitted to the runtime.

Use terminal/tmux scrollback, selection, and search for conversation history.
The conversation has no alternate screen or application-owned viewport; only the
temporary tool inspector uses the alternate screen. Resizing
does not re-render completed responses; reflow is up to the terminal, and explicit
line breaks remain. Known limitation: narrowing a pane during streaming can leave
a copy of the unfinished line in scrollback when prompt_toolkit erases its old
layout after the terminal has reflowed the input frame. A tmux regression test
tracks this as an expected failure; committed lines are not reprinted.

Editor history remains in memory; live model messages and transcript events are
saved unless `--no-save` is set. Failed/cancelled runs recover settled checkpoints
when safe. Cancellation never undoes completed tool effects.
