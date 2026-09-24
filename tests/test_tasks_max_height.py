"""`tasks_max_height` caps the task widget and editor box together."""

import asyncio
from io import StringIO

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.formatted_text.utils import fragment_list_to_text
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console

from pcode.commands import CommandRegistry
from pcode.preferences import SETTINGS, parse_height
from pcode.ui import Activity, Transcript, create_prompt

ROWS = 40


def render(cap: float | None, *, attach: bool = False) -> tuple[int, int]:
    """Task rows shown and editor text rows, for a long plan and a long draft."""

    async def run():
        stream = StringIO()
        activity = Activity(tasks_max_height=cap, attach_tasks=attach)
        activity.plan = [{"content": f"TASK_{i}", "status": "pending"} for i in range(40)]
        view = Transcript(Console(file=stream), activity=activity)
        with create_pipe_input() as pipe:
            session = create_prompt(
                CommandRegistry(),
                activity=activity,
                transcript=view,
                on_submit=lambda text: None,
                input=pipe,
                output=Vt100_Output(stream, lambda: Size(rows=ROWS, columns=60), enable_cpr=False),
            )
            session.default_buffer.text = "\n".join(f"line {i}" for i in range(60))
            app = session.app
            with set_app(app):
                try:
                    app.renderer.render(app, app.layout)
                    tasks = sum(
                        fragment_list_to_text(to_formatted_text(w.content.text)).count("TASK_")
                        for w in app.layout.find_all_windows()
                        if w.render_info is not None and isinstance(w.content, FormattedTextControl)
                    )
                    return tasks, app.layout.current_window.render_info.window_height
                finally:
                    await app.cancel_and_wait_for_background_tasks()

    return asyncio.run(run())


def test_unset_keeps_the_default_layout():
    assert SETTINGS["tasks_max_height"].default is None
    tasks, editor = render(None)
    assert tasks == 5
    assert editor > ROWS // 2  # The editor grows into the rest of the screen.


@pytest.mark.parametrize("attach", [False, True])
@pytest.mark.parametrize("cap", [0.5, 20.0])
def test_a_cap_bounds_tasks_and_editor_together(cap, attach):
    tasks, editor = render(cap, attach=attach)
    chrome = 3 if attach else 4  # Editor borders, plus the tasks' frame or divider.
    assert tasks > 5  # The tasks fill the room they were given...
    assert editor >= 1  # ...leaving the editor a row to type in.
    assert tasks + editor + chrome == 20


@pytest.mark.parametrize(
    "value,parsed",
    [("0.5", 0.5), ("12", 12.0), ("1", 1.0), ("1.5", None), ("0", None), ("-0.5", None)]
    + [("nan", None), ("inf", None), ("half", None), ("", None)],
)
def test_parse_height(value, parsed):
    assert parse_height(value) == parsed
    if value:
        if parsed is None:
            with pytest.raises(ValueError, match="fraction"):
                SETTINGS["tasks_max_height"].validate("tasks_max_height", value)
        else:
            SETTINGS["tasks_max_height"].validate("tasks_max_height", value)
