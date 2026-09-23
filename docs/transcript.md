# The transcript

## Command previews

Shell tool calls show a compact two-row preview with their result and duration.
Long arguments and embedded scripts are abbreviated; short commands remain readable.
Previews are redacted and terminal-control sanitized. Failure excerpts remain visible.
There is currently no command to expand previews or show full command outputs.

## Edit diffs and streaming previews

Completed `edit_file` and `write_file` calls show compact unified diffs in
scrollback by default. These compare the contents used by the operation, not
Git's working-tree diff, so they don't fold in earlier user changes. New files
are marked as created; unchanged files have no patch. Failed calls aren't
presented as successful edits.

While the model generates a file-tool call, a bounded preview shows the proposed
replacement or write content, labeled **not applied**. It only exposes completed
lines from incomplete arguments. This preview uses the live panel's shared
height budget (`command_preview_lines`), independently of command-output
visibility. It disappears on execution, cancellation, or failure and is never
saved as an applied change. Providers that send arguments all at once may have
no visible streaming phase.

```text
/show-edits off   Hide edit blocks and previews, and redraw retained scrollback
/show-edits on    Show them again, including previously hidden completed diffs
/show-edits       Toggle visibility
```

The choice is saved for the next launch. `pcode config set show_edits on|off`
also sets the startup default. Like `/redraw`, toggling rebuilds terminal history;
it does not rerun tools or change files.

Completed diffs are saved in the session journal and restored with the recent
transcript on resume, even if the files have since changed. Hidden diffs are
still retained; hiding is not deletion. Old sessions without captured diffs
continue to show their existing transcript without reconstructing file changes.

Diff capture omits sensitive paths, redacts recognizable credentials and terminal
controls, and bounds file inputs to 256 Ki characters / 4,000 lines. Saved previews
are capped at 400 patch lines / 64 Ki characters; each displayed block shows at
most 60 wrapped patch rows, with an omission marker. Binary, unreadable, and large
before-snapshots get an explicit unavailable notice instead of a misleading patch.
These previews aren't guaranteed to be applicable patches. Shell commands,
formatters, external writers, and other tools are outside this capture mechanism.

## Saved thinking in scrollback

Press **Ctrl+T** or use `/show-thinking [on|off]` to show or hide provider-exposed
thinking text. When enabled, thinking streams into normal terminal scrollback in
a muted, dim style, distinct from the answer. Complete lines are printed as they
arrive; the unfinished last line is flushed at the block boundary or when a turn
ends, fails, or is cancelled. Thinking no longer appears in the Tasks/Tools
header or a separate live panel.

The toggle saves the default and triggers the same retained-transcript rebuild
as `/redraw`, so it reveals or hides earlier thinking too, including text received
while hidden. It works during a turn and after completion, preserves the editor
draft, and does not duplicate answer/tool output. The usual redraw limitations
apply: regeneration clears pre-pcode terminal history and projects the bounded
retained transcript, not an unlimited terminal archive. Redirected output cannot
be retroactively erased or redrawn.

```sh
pcode config set show_thinking on   # Default is off
```

**Privacy and persistence:** readable thinking is recorded in saved sessions even
when hidden. Resuming a session restores thinking alongside its retained transcript;
the complete text remains in the session journal. Interrupted blocks are retained
too. Display redaction is not redaction of session files. Provider signatures and
redacted/opaque thinking blocks are not printed or added to the readable-thinking
journal events, but native model-message history may still contain them for
continuation. `--no-save` disables session persistence, not in-process replay or
terminal scrollback. Turning visibility off is not secure deletion of terminal,
log, or session history. Older sessions without thinking journal events cannot
retroactively populate this view from their native model history.

There is no longer an 8,192-character thinking tail or a thinking-row limit.
Legacy `thinking_display` and `thinking_lines` preferences are ignored. Provider
summaries may themselves be abbreviated: this view shows the readable text the
provider actually exposes, **not hidden internal reasoning**. Delegated agents'
thinking is still not forwarded into the parent's transcript.

For direct Anthropic models, enabling the view also requests visible thinking on
the next turn: adaptive thinking for supported models, otherwise an explicit
2,048-token legacy budget, with `display: "summarized"`. This can increase latency
and token usage. Turning it off removes that request override and restores provider
defaults, rather than explicitly disabling reasoning. In-flight requests and the
separately selected effort are unchanged. Codex requests `summary: "auto"`
independently of visibility. Meridian still needs upstream thinking generation and
thinking forwarding; pcode does not mutate an external proxy's global settings.
See the Meridian setup section for the isolated managed-instance option.

## Error logs in scrollback

Errors and failed-tool diagnostics render as fenced Markdown code blocks using
Rich and the active code theme. Logs stay literal, even if they contain Markdown
or backticks. By default, each error shows at most **20 wrapped body lines**, plus
its heading and two code-block padding rows. Long logs keep their tail and include
a truncation marker within that limit.

```sh
pcode config set error_scrollback_lines 40  # Positive integer; default 20
```

This line limit also works through `/config` and applies on the next launch.

By default, a failed tool call does not write its diagnostic to scrollback. It
keeps the same compact summary line a successful call leaves, marked `✗` in the
error colour instead of `✓`, so the failed call stays visible without its log.
Enable `tool_error_scrollback` for the full diagnostic:

```sh
pcode config set tool_error_scrollback on  # Failed-tool diagnostics (default off)
```

Command failures still follow command visibility below: with mirroring off they
keep only their summary line, and with mirroring on they show that line plus the
captured output, the output arriving only once this option is on. Application
errors remain visible either way, as do warnings and cancellation notices.
Saved diagnostics are not disabled or trimmed by these display settings.
Command diagnostics retain a separate safety bound of 200 lines / 32,000
characters, after redaction.

## Command output in scrollback

By default, a settled command leaves the same compact summary line every other
tool leaves, with the command preview inline after the elapsed time. Long summaries
truncate to the terminal width instead of wrapping. Captured output stays in the
mutable tool panel. Background completion notices use `Run(bg j12)`, keeping the
job id beside the label and the exit status and elapsed time after it. Enable
`show_commands` to mirror **every settled shell tool call and its captured
output** into permanent terminal scrollback (a failed call mirrors its output
only with `tool_error_scrollback` on):

```sh
pcode config set show_commands on             # Mirror commands and output (default off)
pcode config set command_scrollback_lines 80  # Positive integer; default 20
pcode config set command_preview_lines 10     # Live output height cap; default 10
pcode config set show_commands off            # Summary lines only (default)
```

Each mirrored block shows a success/failure indicator, the tool label, the job
id, elapsed time, and a shell-highlighted invocation on a `$` line. A finished
job's `[jN · exit C · elapsed]` line is dropped from the mirrored output, since
the heading already carries all three; an unfinished job's marker, which also
names its pid and how to get back to it, stays put. Captured output stays
literal, with its indentation preserved and no Markdown parsing or extra block
padding. Process polling details without a command are shown without a `$` prefix:

The heading sits on the block's opening line, and a plain line closes it:

```text
✓ Run · j7 · 0.4s ─────────────────────────────────────────
  $ pytest -q
  2 passed in 0.31s
────────────────────────────────────────────────────────────
```

Details:

- It covers the current `shell` tool, including delegated calls. Saved legacy
  `run_command`, `start_command`, `check_command`, and `stop_command` entries also
  remain displayable. Other tools are unaffected.
- Active foreground `shell` calls show a preview above the prompt, refreshed as
  complete lines arrive from the combined stdout/stderr log. Harness emits at most
  the first 16,000 bytes; a capped preview is marked, and further output stays in
  the command log. Ctrl+G controls both preview and scrollback.
  The live block is drawn like the settled one: the same heading on an opening
  line (with `⟳` and the elapsed time, since nothing has finished yet), the same
  indented `$` line, and a closing line instead of a box.
  `command_preview_lines` caps the live output at 10 wrapped rows by default
  (positive integer, excluding the command and block lines). The preview uses
  space left after the editor, queued prompts, and Tasks/Tools panel. Under tight
  height pressure, task rows yield only enough to retain a one-line output tail.
  For parallel calls,
  the most recently updated command is shown; all calls remain in the tool panel.
  Programs that buffer their own output must flush it (for example, `python -u`).
- On completion the transient preview disappears and one settled block is
  written to scrollback, without duplicate streamed lines. Preview updates are
  not saved in session history. Background `shell` calls return PID/log/status
  handles rather than streaming output after the call ends.
- Failed commands follow the same show/hide setting as successful commands.
  When shown, they print one block containing captured output (or the saved
  diagnostic if output is unavailable). There is no separate error visibility option.
- Output is redacted and sanitized before display, then bounded to
  `command_scrollback_lines` wrapped output rows (default 20), taken from the end.
  An upstream-truncated tail may start inside a credential with its opening marker
  missing. In that case pcode omits the tail from scrollback and inspection, keeps
  the process/log/status handles, and leaves the raw model result unchanged.
  A separate omission marker counts omitted rendered rows. The command, marker,
  and subtle top/bottom borders are outside this budget, so even a budget of one
  retains the final output row. The capture step retains its own 128 KiB payload bound.
- Verbose commands can push earlier conversation out of terminal history, so
  raise your terminal or tmux scrollback limit before enabling this.

Press **Ctrl+G** to turn mirroring on or off for the rest of the session; it also
saves the default, so the next launch starts in the state you left.
`/show-commands on` and `/show-commands off` do the same, and bare `/show-commands`
toggles. Toggling rebuilds the retained scrollback immediately:
turn it on to reveal earlier captured commands and their outputs; turn it off to
remove all command blocks, including failures. No commands
are rerun. Future completions use the same setting.

Ctrl+G toggles command output outside history search; inside search it retains
its native cancel behavior. Ctrl+S now cycles send modes instead of opening
forward incremental search. Ctrl+R still opens history search. prompt_toolkit
disables terminal XON/XOFF flow control while the prompt is active, so Ctrl+S
reaches the application instead of pausing terminal output.

These settings also work through `/config` and apply on the next launch.

## Paced scrollback

Model text reaches scrollback one settled Markdown block at a time, and a long
code block or list would otherwise land in a single frame. By default each
settled block is rendered once and then written a few rows per frame, so it
rolls out instead of appearing all at once. Small blocks reveal one row per
frame; large ones go faster, so a block is fully written within about a second
of settling however big it is. Rendering, ordering, and the transcript retained
for resume or redraw are unchanged; a redraw or resize rebuild always lands
whole. To write every block in one frame:

```sh
pcode config set paced_scrollback off  # Default on; applies on next launch
```

## Regenerating the terminal transcript

`/redraw` rebuilds the retained transcript at the current terminal width and with
current display settings. Ctrl+G, `/show-commands`, `/show-edits`,
`/theme`, `/colors`, and `/syntax`
use the same replay mechanism. The draft, active tool panel, and unfinished model
text are preserved; replay neither calls tools nor changes model history.

The transcript also rebuilds automatically after a terminal size change settles.
Height changes rebuild history too, so live preview fragments do not remain in
scrollback after the pane shrinks. To disable automatic replay:

```sh
pcode config set regenerate_on_resize off  # Default on; applies on next launch
```

Resize replay is debounced to avoid rebuilding on every intermediate size during
a drag. Without it, the editor still resizes normally; `/redraw` remains available
for an explicit transcript reflow.

Closing an alternate-screen popup (modal dialog), such as `/tree`, `/links`,
`/model`, `/resume`, `/status`, `/tools`, or `/diffs`, also rebuilds the retained
transcript. This restores the conversation even if the terminal lost its previous
screen contents. Dismissal with Escape does the same; the editor draft is preserved.
Popup restoration is independent of `regenerate_on_resize`.

**Terminal-history warning:** regeneration clears the terminal's visible screen
and scrollback, including shell output from before pcode started. It then rebuilds
only the transcript retained by this pcode process. This uses the normal-screen
ANSI erase-scrollback sequence (verified in tmux); terminals that ignore that
sequence may leave older copies in history. Redirected/non-terminal output is not
cleared or redrawn; resume prints the retained slice once without terminal escapes.

Resume (`--continue` or `/resume`) loads the selected conversation path from disk
into the same retention log, then performs a redraw. Switching branches with
`/tree` also replaces the displayed history rather than appending another preview.
Thinking, edits, and saved command results are retained even when hidden, so later
visibility toggles work on resumed history too. Tools are never re-executed.

One setting controls the text budget for both live redraw and resume:

```sh
pcode config set transcript_max_chars 2000000  # Default; applies on next launch
```

The budget counts estimated retained text characters, not rendered lines, bytes,
or model tokens. Oldest entries are evicted first; the newest entry is kept even
if it alone exceeds the budget. A tiny-write guard scales with the same setting
(one entry per 100 budget characters, at least one entry; **20,000 entries** at the
default). If history was evicted, redraw shows an omission notice. Saved sessions,
model context, and diagnostics are unaffected. Ordinary redraw does not reread
the archive; resume rebuilds the retained slice from it.
