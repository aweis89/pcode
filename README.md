# pcode

A small, offline UI preview of [PLAN.md](PLAN.md). Judge the prompt, completion
menu, and transcript before adding a real agent runtime.

## Run

With [uv](https://docs.astral.sh/uv/) installed:

```sh
uv run pcode
```

Type `/` to open the command menu, then narrow it by typing. Use Tab or the
arrow keys to choose. Enter accepts a selected completion; another Enter runs it.

- `/demo`: fictional coding response with Markdown, Python, a diff, a table,
  and completed tool summaries. No actual tools run.
- `/theme light` or `/theme dark`: change the input and future output palette.
  `/theme` alone toggles. Start in light mode with `uv run pcode --theme light`.
- `/help`: command list and keyboard shortcuts.
- `/context`: preview counter and an honest list of what is not connected.
- `/new`: reset the preview counter without clearing scrollback or input history.
- `/quit` (alias `/exit`): exit.

Ordinary messages get a canned reply. No model, API key, network call, filesystem
access, or shell tool is involved at runtime. Input history lives only in memory.

### Keys

| Key | Action |
| --- | --- |
| Enter | Send, or accept a selected completion |
| Alt+Enter | Insert a newline (Esc followed by Enter also works) |
| Tab / arrows | Browse completion; arrows also navigate input/history |
| Ctrl+R | Search this process's input history |
| Ctrl+C | Discard the current input |
| Ctrl+D | Exit when the input is empty; otherwise forward-delete |

The input starts with one editable line (plus its border), expands for wrapped
text or explicit newlines, and shrinks again when text is removed. Completion
appears below the frame instead of enlarging it. Very long input scrolls within
the available pane height.

Multiline bracketed paste is supported. Mouse capture is off, so normal terminal
selection remains available. For a non-interactive rendering sample:

```sh
uv run pcode --demo
```

## Deliberately small architecture

- `src/pcode/ui.py`: `PromptSession` editing with a content-sized layout built
  from public prompt_toolkit widgets, plus Rich transcript rendering. Normal
  screen only, no custom cursor handling.
- `src/pcode/commands.py`: metadata registry shared by execution, help, and
  completion (including theme arguments).
- `src/pcode/runtime.py`: deterministic fixture runtime returning plain events;
  it imports neither terminal library.
- `src/pcode/app.py`: composes the preview and its commands.

The mutable prompt is erased on submission, then Rich prints the submitted input
and completed response once. Historical output is never repainted. The toolbar
belongs to prompt_toolkit's temporary prompt area, not an independently pinned bar.
Code blocks use Rich's bundled syntax highlighting; prose inherits the terminal's
foreground/background. Theme changes do not recolor existing scrollback.

This is **not Milestone 1 of the full plan**: streaming, real Pydantic AI/Harness
integration, approvals, persistence, pickers, and actual tools are intentionally
omitted. The next slice can replace the fixture runtime with a Pydantic adapter
without teaching the renderer about framework-specific events. Streaming will
need its own terminal-stability validation rather than a pretend animation here.

## API references used

- [prompt_toolkit prompts](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/asking_for_input.html):
  `PromptSession`, custom completion metadata, multiline editing, key bindings,
  frames, and temporary toolbars.
- [Upstream toolbar example](https://github.com/prompt-toolkit/python-prompt-toolkit/blob/main/examples/prompts/bottom-toolbar.py):
  use the library-owned toolbar instead of hand-positioning a status line.
- [Rich Console](https://rich.readthedocs.io/en/stable/console.html): normal
  `Console.print` with automatic terminal width and permanent output.
- [Pydantic AI](https://ai.pydantic.dev/) and
  [Harness](https://ai.pydantic.dev/harness/): reviewed for the later runtime;
  neither is installed just to display canned responses.

## Validate

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Tests cover registry dispatch, completion, multiline/paste keys, history search,
interruption, Unicode and narrow output, and real Unix PTY startup/resize/exit.
When tmux is installed, isolated-server tests also measure input height across
horizontal/vertical splits, completion, line wrapping, newlines, and deletion.
These include real cursor-position reports; plain PTYs alone missed the original
frame-stretching bug. Tests also check that the app does not switch to the alternate
screen, erase scrollback, or set a scroll region. They do not prove visual
correctness in every emulator.

For a manual feel check, run `uv run pcode` inside tmux, try `/demo`, resize the
pane, then use copy mode/search to find earlier output. Exit and check that the
transcript remains. Actual tmux copy-mode and emulator rendering still need that
manual check.
