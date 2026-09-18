"""Preview allocation with real editor wrapping; CPR is covered in tmux tests."""

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
from pcode.preferences import save_preferences
from pcode.runtime import CommandOutput
from pcode.ui import Activity, Transcript, create_prompt


@pytest.mark.parametrize("height", [14, 24, 40])
@pytest.mark.parametrize("cap", [1, 10])
@pytest.mark.parametrize("tasks", [False, True])
@pytest.mark.parametrize("draft,queued", [("", 0), ("one\ntwo\nthree", 3)])
def test_preview_shares_space_with_actual_editor_and_queue(height, cap, tasks, draft, queued):
    save_preferences(command_scrollback="on", command_preview_lines=str(cap))

    async def run():
        stream = StringIO()
        activity = Activity(show_tasks=tasks, queued_prompts=["queued"] * queued)
        activity.start_prompt("Run tests")
        activity.plan = [{"content": f"TASK_{i}", "status": "in_progress"} for i in range(5)]
        activity.command_outputs["one"] = CommandOutput(
            "one", "noisy", "\n".join(f"OUTPUT_{i:02}" for i in range(60))
        )
        view = Transcript(Console(file=stream), activity=activity)
        with create_pipe_input() as pipe:
            session = create_prompt(
                CommandRegistry(),
                activity=activity,
                transcript=view,
                on_submit=lambda text: None,
                input=pipe,
                output=Vt100_Output(
                    stream, lambda: Size(rows=height, columns=40), enable_cpr=False
                ),
                bottom_toolbar=[("", "status")],
            )
            session.default_buffer.text = draft
            app = session.app
            with set_app(app):
                try:
                    app.renderer.render(app, app.layout)
                    visible = []
                    for window in app.layout.find_all_windows():
                        if window.render_info is not None and isinstance(
                            window.content, FormattedTextControl
                        ):
                            text = fragment_list_to_text(to_formatted_text(window.content.text))
                            visible.append(text)
                            if "$ noisy" in text:
                                preview_control = window.content
                    preview = next(text for text in visible if "$ noisy" in text)
                    assert preview.endswith("OUTPUT_59")
                    assert 1 <= preview.count("OUTPUT_") <= cap
                    assert "Command output" not in stream.getvalue()
                    # No stretching to fill otherwise unused space.
                    editor = app.layout.current_window
                    assert 1 <= editor.render_info.window_height <= draft.count("\n") + 1
                    if height == 40:
                        assert preview.count("OUTPUT_") == cap
                        if tasks:
                            assert sum(text.count("TASK_") for text in visible) == 5
                    # Turning off the shared flag removes the preview too.
                    view.command_scrollback = False
                    app.renderer.render(app, app.layout)
                    assert not fragment_list_to_text(to_formatted_text(preview_control.text))
                finally:
                    await app.cancel_and_wait_for_background_tasks()

    asyncio.run(run())
