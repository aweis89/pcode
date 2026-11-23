"""Autowrap stays off while the editor is painted, so Ghostty never reflows it.

A narrowing resize otherwise splits the editor's full-width rows in two and
pushes its top up the pane until the resize repaint. Transcript written
between an erase and the next paint must still wrap.
"""

from types import SimpleNamespace

from pcode.ui import install_nowrap_editor


class Renderer:
    """prompt_toolkit's calls, as far as autowrap goes."""

    def __init__(self, log):
        self.log = log
        self.output = None

    def render(self, app, layout, is_done=False):
        self.output.disable_autowrap()
        self.log.append("paint")
        self.output.enable_autowrap()

    def reset(self, _scroll=False, leave_alternate_screen=True):
        self.log.append("reset")

    def erase(self):
        self.output.enable_autowrap()
        self.reset()


def painted():
    log = []
    output = SimpleNamespace(
        disable_autowrap=lambda: log.append("nowrap"),
        enable_autowrap=lambda: log.append("wrap"),
        flush=lambda: None,
    )
    renderer = Renderer(log)
    renderer.output = output
    install_nowrap_editor(SimpleNamespace(renderer=renderer, output=output))
    return renderer, log


def test_paints_leave_autowrap_off():
    renderer, log = painted()
    renderer.render(None, None)
    renderer.render(None, None)
    assert log == ["nowrap", "paint", "nowrap", "paint"]


def test_erase_turns_autowrap_back_on_for_the_transcript():
    renderer, log = painted()
    renderer.render(None, None)
    renderer.erase()
    log.append("transcript")
    assert log == ["nowrap", "paint", "reset", "wrap", "transcript"]


def test_reset_without_a_paint_does_not_touch_autowrap():
    renderer, log = painted()
    renderer.reset()
    assert log == ["reset"]


def test_a_final_paint_turns_autowrap_back_on():
    renderer, log = painted()
    renderer.render(None, None)
    renderer.render(None, None, is_done=True)
    assert log[-1] == "wrap"


def test_real_renderer_and_output_leave_autowrap_off_until_an_erase():
    """The overrides take effect on pcode's real output and prompt_toolkit's renderer."""
    from io import StringIO

    from prompt_toolkit.application import Application
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.layout import Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.output.vt100 import Vt100_Output

    from pcode.ui import CursorSafeOutput

    stream = StringIO()
    stream.isatty = lambda: False
    output = CursorSafeOutput(Vt100_Output(stream, lambda: Size(rows=10, columns=40), term="xterm"))
    with create_pipe_input() as pipe:
        app = Application(
            layout=Layout(Window(FormattedTextControl("─" * 40))), output=output, input=pipe
        )
        install_nowrap_editor(app)
        app.renderer.render(app, app.layout)
        painted = stream.getvalue()
        assert painted.rfind("\x1b[?7l") > painted.rfind("\x1b[?7h")
        app.renderer.erase()
        erased = stream.getvalue()[len(painted) :]
        assert "\x1b[?7h" in erased
