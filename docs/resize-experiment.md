# Resize-only clearing experiment

## Reproduction

The empty-input reproduction is
`tests/test_tmux.py::test_empty_input_resize_preserves_transcript_without_task_ghosts`.
It uses a local fake runtime, real tmux cursor-position reports, a task with
recent tool activity, and 40 numbered committed transcript markers. No model or
credentials are needed. It starts at 240 columns by 40 rows, shrinks to 120 by
24, and checks both the screen and scrollback. The remaining resize sequence
exercises expansion and a larger width reduction once the first failure is fixed.

Run from the repository root:

```sh
.venv/bin/python -m pytest tests/test_tmux.py -k empty_input_resize --runxfail -q
```

The current implementation retains all transcript markers but leaves a partial
old task panel above the new panel. The test is a strict expected failure, not a
claim that resizing is fixed. A multiline draft is not required.

## Prototypes evaluated

Verified installed prompt_toolkit: 3.0.53; Rich: 14.3.4.
The prototypes were injected only into local test subprocesses; neither changes
production behavior.

### Clear the viewport, reset, and repaint

The experimental replacement for `Application._on_resize` was:

```python
def resize(self):
    self.output.cursor_goto(0, 0)
    self.output.erase_down()
    self.output.flush()
    self.renderer.reset(leave_alternate_screen=False)
    self._request_absolute_cursor_position()
    self._redraw()
```

This uses the existing `CursorSafeOutput.erase_down` wrapper rather than ED3,
a terminal reset, or a tmux history-clear command. It removes the visible task
duplicate. However, it also erases committed transcript that is still visible:
in the regression above, `RESIZE_TRANSCRIPT_039` disappears from both the screen
and history. In a separate run with more transcript visible before the first
resize, only 13 of 40 committed markers survived.

Retaining scrollback is not enough: some permanent output still resides on the
visible screen. The current transcript output handoff drains pending Rich
objects; it does not keep a viewport replay model. Resetting the prompt renderer
can repaint the widgets and live preview, but not those committed lines.

### Clear only the estimated reflowed live region

A second prototype adjusted the renderer's remembered cursor distance before
its normal resize erase, counting the extra physical rows produced by narrowing
each old rendered row. This preserved the transcript and removed the duplicate
for a 240-to-120-column shrink. A subsequent 240-to-80-column shrink still put
an old task copy in scrollback; expansion made it visible again.

That calculation is also terminal-dependent: a renderer cell grid does not
fully describe terminal wrap flags and history movement. It is not a safe
general-purpose replacement for terminal state tracking.

## Decision

Do not enable either prototype in production. Keep the regression as an explicit
known failure. A whole-viewport redraw needs a transcript ownership/replay design
that preserves committed output exactly once, not just another invalidate or
screen erase. Never clear terminal history to remove mutable-widget artifacts.
