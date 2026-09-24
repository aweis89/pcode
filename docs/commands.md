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
plus untracked, honoring `.gitignore`). Outside a Git checkout it comes from
ripgrep, which skips hidden entries and honors `.ignore` files, with build
directories such as `node_modules` excluded on top; a pure-Python directory walk
covers a machine with no ripgrep. The listing is refreshed at most every ten
seconds, and completion blocks the editor while it runs, which is why the fast
path matters on a large tree.

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
  output). Auto is the built-in default; saved theme choices still take precedence.
  Restart pcode after changing your terminal background. Restore auto mode
  with `/theme auto` or `pcode config set theme auto`.
  `/theme` alone toggles. The palette decides which `/syntax` setting applies
  (`syntax_dark` or `syntax_light`); normal body text and the overall background
  stay terminal-native either way.
- `/syntax NAME`: choose the colors for the active palette and save them as that
  palette's default; `/syntax` alone reports the current choice.
  `/syntax terminal` is the default: scrollback, fenced code (`ansi_dark` /
  `ansi_light`), the prompt, task rows and the completion popup all use the
  terminal's own ANSI colors, so pcode follows whatever scheme the terminal runs.
  Any Pygments style (`/syntax gruvbox-dark`, `/syntax monokai`) switches to
  pcode's own colors instead: headings, links, quotes and tables use the palette,
  and code, popup and prompt are derived from that style. See
  [Code highlighting styles](configuration.md#code-highlighting-styles) for the
  list; `/theme-preview` renders every style, marking the one in use. Retained
  scrollback is rebuilt with the new colors, just like `/redraw`.
- Session, conversation-tree, model, and tool popups share terminal-default
  backgrounds and text, with reverse-video selection highlights. They follow your
  terminal background automatically, independently of `/theme` and `/syntax`.
- `/help` (or `/commands`): grouped command list and keyboard shortcuts.
- `/login [anthropic|openai-codex|meridian]`: sign in in a browser. Anthropic is pcode's own flow;
  `openai-codex` uses Pydantic AI's OAuth flow (no CLI required); `meridian` runs
  `claude auth login` for the login your Meridian proxy reads ([details](providers.md#signing-in)).
  `/logout [anthropic|openai-codex]` removes pcode's stored login, leaving CLI credentials untouched. Both require an idle conversation.
- `/model`: searchable model picker for configured providers (keeps the conversation;
  applies from the next request when chosen mid-run).
- `/tools`: scrollable tool-call inspector for the current conversation, including resumed calls.
- `/tools failed`: open the same inspector filtered to failures.
- `/diffs`: browse this conversation's file diffs in a full-screen popup.
- `/links`: pick a URL from the active conversation branch (your prompts, tool
  arguments and captured results, or the assistant's replies, last appearance first).
  Tool links show the tool name; duplicate URLs appear once, at their most recent
  position. Press `t` to show/hide tool links (shown by default); URLs also present
  in prompts or replies remain when tools are hidden. Press `/` to search URLs,
  labels, and sources, case-insensitively. Search filters as you type; `↑`/`↓`
  move the selection, `Enter` returns to the list with the filter applied, and
  `Esc` clears the search and returns to the list. In the list, `Enter` opens the
  selected URL and `Esc` closes the picker. Filters reset when you reopen `/links`.
  Output omitted by truncation or stored only in a spill file is not searched. Open the selection
  in the default browser via
  `open` (macOS), `xdg-open` (Linux), or the shell association (Windows). Useful
  when the terminal or an older tmux does not make rendered links clickable.
- `/status`: current model, workspace, session storage path, completed turns, token usage,
  and a breakdown of the prompt overhead the model is re-sent every request — see
  [Where the fixed prompt goes](context.md#where-the-fixed-prompt-goes). Opens a popup in the
  interactive editor; prints inline when there is no editor.
- `/resend`: retry from the last checkpoint without a new message; shows the previous prompt and spinner.
- `/jobs [list|stop ID|stop all|watch ID|unwatch]`: shell commands still running, how to stop
  them, and pinning one's output tail into the command preview. Jobs outlive the turn that
  started them — see [Shell jobs](tools.md#shell-jobs).
- `/compact [focus]`: summarize older context with the current model; keep recent history.
- `/autocompact on|off`: toggle automatic LLM compaction (saved user preference; default on).
- `/new`: start a new saved conversation; clears the screen and retained scrollback, keeps input history.
- `/resume`: browse and search saved conversations by their prompts; resume one in place.
- `/tree`: [browse and fork the conversation](conversation-tree.md); select a user prompt to
  edit it, or an assistant response to continue from there. Existing branches are kept.
  Readable at any time; forking waits for the running turn.
- `/btw QUESTION`: [ask a side question](side-questions.md) against the context the model is
  working with right now, without interrupting or queueing it. The answer opens in a popup
  when it is ready (`btw_auto_open`); a bare `/btw` opens the answers at any time.
- `/skill:NAME [text]`: run a discovered skill; see
  [Skills as slash commands](workspace.md#skills-as-slash-commands) for naming and configuration.
- `/quit` (alias `/exit`): exit.

## Keys and layout

| Key | Action |
| --- | --- |
| Enter | Send using the active send mode, or accept a selected completion |
| Ctrl+S | Cycle steering → queue → interrupt for the next send only |
| ↓ | Newline when on the last line with nothing to complete or recall (works in vi insert mode) |
| Ctrl+J / Shift+Enter | Newline; see [Newlines in tmux](#newlines-in-tmux) if neither reaches pcode |
| Alt+Enter | Newline in Emacs mode only (Esc followed by Enter also works) |
| Tab / arrows | Browse completion; arrows also navigate input/history |
| Ctrl+L | Choose a model (keeps the conversation; applies from the next request) |
| Ctrl+O | Show/hide the Tasks/Tools widget (saves the default) |
| Ctrl+R | Search this process's input history |
| Ctrl+Y | Copy the current draft to the system clipboard (collapsed pastes are expanded first) |
| Ctrl+G | Mirror commands and their output to scrollback (saves the default) |
| Ctrl+C | Discard input; cancels the running turn only when the prompt is empty |
| Ctrl+D | Exit on empty idle input; cancel during generation |

Press **Ctrl+O** or use `/show-tasks [on|off]` to hide or show the Tasks/Tools
widget without stopping work or clearing task/tool history. The current prompt
and queue remain visible. Visibility is saved across launches (default: on);
use `pcode config set show_tasks off` to set the default from the shell.

Delegated sub-agents are listed in the widget like tasks. A finished delegate
stays with a ✓ (or `!` if it failed), along with its own task list, until the
next turn starts.

`/autohide-tasks on` (or `pcode config set autohide_tasks on`) hides the widget
as soon as the model finishes a turn, keeping the idle prompt compact; it
returns on the next turn, and Ctrl+O brings it back immediately. Default: off.

`/attach-tasks on` (or `pcode config set attach_tasks on`) draws the widget as
the top of the editor box instead of a separate box above it: its heading
becomes the editor's top border and a divider separates the tasks from your
draft. Queued prompts then sit above the combined box. Default: off.
Ctrl+O replaces the editor’s insert-newline binding; Ctrl+J still inserts a newline.

**Ctrl+Y** copies whatever is in the editor right now, so a draft can be moved
somewhere else without sending it. A collapsed paste marker is expanded first:
what lands on the clipboard is what Enter would send. It replaces `yank` in
Emacs editing mode (`Ctrl+X r y` still pastes from the kill ring) and
copy-character-from-above in vi insert mode. Copying uses a
local helper (`pbcopy`, `wl-copy`, `xclip`) or OSC 52 over ssh, the same as the
popups, and truncates at 64 KiB.

**Setting acknowledgements are transient.** Toggles and display settings
(`/show-thinking`, `/show-tasks`, `/show-edits`, `/show-commands`,
`/autohide-tasks`, `/autocompact`, `/theme`, `/syntax`, `/effort`)
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

A running `delegate_task` keeps its own row, and a sub-agent that plans shows up
to three of its tasks indented beneath it, centred on its active task, with its
current tool calls nested under that task the same way. The sub-agent's plan is
separate from yours: it is never saved, never merged into your plan, and leaves
the widget when the delegate finishes. The built-in worker always plans this way;
an extension's delegate opts in by giving its agent `IdentifiedPlanning()` from
`pcode.planning` (see "Sub-agents" in `src/pcode/extension_guide.md`).

```text
* Fix the flaky login test
    ⟳ Delegate · Working · 12.4s · worker · Investigate the retry path
        ✓ Read the retry code
        * Reproduce the failure
            ⟳ Run · 1.2s · pytest -q tests/test_login.py
        ○ Report back
```

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
example `12.5k/200k` (tokens used / effective working window).

Used context is the **latest completed request's input tokens**, including cached
input, not cumulative session usage. It updates as each request completes, so it
moves during a long tool loop rather than only at the end of the turn, and it
follows the selected conversation history when resuming or navigating branches.
It does not include unsent drafts, subsequent tool results, or a response still
streaming.
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
`steering` → `queue` → `interrupt` for the *next* send only: the status bar
shows the picked mode with `(once)` next to it, and the saved default comes back
as soon as a prompt is sent. Mode and working status take
priority over model and path metadata in narrow panes. Existing queued messages
keep their submission mode.

- **steering**: deliver input at the next model request, after active tools finish.
  A shell command the turn is waiting on does not hold that request back: the
  wait ends and hands the model a [job](tools.md#shell-jobs) handle, and the
  command keeps running. Pending input is labeled “Steering (next model
  request)”; once delivered, it replaces the active prompt in the task bar. If
  the turn finishes before then, send it as a follow-up turn.
- **queue**: wait for the current turn to finish, then start a follow-up turn.
- **interrupt**: cancel the current turn, discard pending messages, and send the
  new message after cancellation cleanup completes. A shell command the turn was
  waiting on keeps running as a [job](tools.md#shell-jobs); you are redirecting
  the model, not cancelling its work. Ctrl+C does stop it.

Set the default with `pcode config set send_mode steering` (or `queue` / `interrupt`).
`/config set send_mode queue` changes the default for the next launch; Ctrl+S
overrides it for one send without changing it. Idle input starts a normal turn in every mode. Slash
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

## Popup keys

Every full-screen popup (`/diffs`, `/tools`, `/links`, `/tree`, `/resume`,
`/btw`, `/status`, and the Ctrl+L model picker) scrolls with the same keys,
acting on whichever pane has focus:

| Key | Action |
| --- | --- |
| ↑ / ↓ | Move the selection in a list, or scroll a text pane by a line |
| PageUp / PageDown | Move or scroll by a page |
| Ctrl+U / Ctrl+D | Move or scroll by half a page |
| Tab / Shift+Tab | Switch panes, where a popup has more than one |
| Esc / Ctrl+C | Close the popup and restore the editor draft |

Ctrl+D never closes a popup; it always half-pages. A list with a search line
keeps these keys working while you type, so the query stays where it is.

Popups capture the mouse by default: clicks select rows and the wheel scrolls
whichever pane is under the pointer, but a plain drag no longer selects text. Most terminals still
select with a modifier held while dragging (usually Shift; Option in iTerm2). In tmux, mouse events reach pcode only with
`tmux set -g mouse on`. To leave the mouse to the terminal instead, so a plain
drag selects text and copy-on-select works:

```sh
pcode config set popup_mouse off
```

Terminals that support alternate scroll mode still turn the wheel into ↑/↓
then, moving the selection or the pane a line at a time. The setting is read as each popup opens, so no restart
is needed.

## Edit diff browser

`/diffs` opens a full-screen popup showing this conversation's completed file
edits, using the same diff colors as scrollback. The diff fills most of the
screen; a small file selector sits at the bottom. Keys are listed in the header:

- Up/Down in the file list selects a file, newest change first.
- Tab/Shift+Tab switch between the file list and the diff. The
  [popup keys](#popup-keys) act on whichever has focus; Ctrl+Home/Ctrl+End jump
  to the first or last line of the diff.
- `/` (or Ctrl+F) opens a search for whichever pane has focus. In the file list
  it filters files by path; in the diff it filters to changes whose diff has a
  matching line and jumps the diff to the first one. Matching is fuzzy: a plain
  substring, or joined word prefixes such as `ed_ui` for `edit_ui.py` or
  `sel_row` for `selected_row`. Every word of the query must match. Up/Down
  still move the file selection while typing; Enter returns to the pane.
  Switching panes and pressing `/` again starts a fresh query for that scope.
- `n`/`N` in either pane jump to the next/previous matching diff line.
- Escape or Ctrl+C closes the popup and restores the editor draft.

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
- In the call list or details, **c** copies the selected call's command (its whole
  arguments payload when it has no command) and **o** copies the returned output.
  Copying uses `pbcopy`/`wl-copy`/`xclip` when one is installed and OSC 52
  otherwise; over ssh it tries OSC 52 first. tmux forwards OSC 52 only with
  `tmux set -g set-clipboard on`. Payloads are truncated at 64 KiB, and the
  header says what was copied or that copying failed.
- In the call list, **f** toggles failures, **t** cycles tool-name filters, and
  **/** focuses search. Search matches tool names/statuses and command/summary
  previews, not the complete output payload. Ctrl+F focuses search from any pane.
- In details, use arrows to scroll by line, PageUp/PageDown by page, or Ctrl+U/Ctrl+D
  by half a page. Ctrl+U/Ctrl+D also half-page the call list, including while
  typing a search. The session browser shares these controls.
- Mouse clicks and wheel scrolling reach the popup unless `popup_mouse` is `off`;
  see [popup keys](#popup-keys) for the text-selection tradeoff.
- Escape or Ctrl+C closes only the inspector and restores the editor draft.
- Wide terminals show calls and details side by side; narrow terminals stack them.

Details include the call/run IDs, timestamp and duration when captured, structured
arguments, framework outcome, and returned output/error. Commands and results are
both shown as blocks, highlighted when the payload is code or JSON and verbatim
otherwise. When `shfmt` is available on `PATH`, shell commands are formatted as
Bash with two-space indentation. Formatting never executes the command. Results
are cached (up to 128 commands); a missing binary, formatting error, or 250 ms
timeout silently falls back to the built-in formatter. The fallback breaks
one-line commands at unquoted top-level `;` and `&&`/`||`, and preserves existing
multiline layout. `shfmt` may keep compact blocks on one line rather than fully
expanding them.

Every logical command line starts with a display-only `$ `, before any
indentation; soft-wrapped rows do not get another marker. **c** still copies the
original command exactly as run, without markers or formatting changes. Nonzero command exits,
timeouts, and tool retries are failures; interruption and unknown results remain
distinct. Command tools show an **Execution** row saying whether the model asked
to wait (`foreground`) or to be handed a job handle (`background`); background
calls are also tagged in the call list, so search matches `background`.
Background launch/check/stop calls show their process ID and related
calls when available. A successful launch is not proof that the process finished
successfully.

Provider-executed web searches (Anthropic's native `web_search`, used when the
`web_search` preference is `auto`) list each hit's title, URL, and age. The page
text itself arrives encrypted for the provider and is replayed to the model on
later requests; there is no client-side key, so the inspector cannot show it.
Local searches (Exa or DuckDuckGo) and `get_page` return plain text and show in
full.

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
messages run in order after the current turn finishes. Ctrl+S cycles the mode for the next send. Slash commands use a separate async
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
