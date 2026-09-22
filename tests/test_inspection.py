import asyncio
from io import StringIO

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output
from pydantic_ai import Agent
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from rich.console import Console

from pcode.agent import create_coder
from pcode.app import PreviewApp
from pcode.inspection import MISSING, PAYLOAD_LIMIT, ToolArchive, capture
from pcode.inspector_ui import ToolInspector
from pcode.live import AgentRuntime
from pcode.runtime import ToolStarted, ToolSummary
from pcode.sessions import SavedSession


def call(archive, identity, *, failed=False, name="run_command", run_id="turn"):
    archive.event(
        ToolStarted(
            name,
            "example",
            identity,
            command="pytest -q",
            arguments='{"command": "pytest -q"}',
            run_id=run_id,
            started_at="2026-01-01T00:00:00+00:00",
        )
    )
    archive.event(
        ToolSummary(
            name,
            "exit 1" if failed else "finished",
            failed=failed,
            call_id=identity,
            elapsed_seconds=0.5,
            result="\n".join(f"output {i}" for i in range(200)),
            run_id=run_id,
            outcome="success",
        )
    )


def test_all_calls_retained_with_filters_and_selection():
    archive = ToolArchive()
    for i in range(25):
        call(archive, str(i), failed=i % 2 == 0)
    call(archive, "plan", name="write_plan")
    with create_pipe_input() as pipe:
        ui = ToolInspector(archive, input=pipe, output=DummyOutput())
        assert len(ui.visible) == 26
        assert ui.selected.name == "write_plan"
        ui.failed = True
        ui.refresh()
        assert len(ui.visible) == 13
        assert ui.selected.call_id == "24"
        ui.list.buffer.cursor_position = ui.list.document.translate_row_col_to_index(1, 0)
        assert ui.selected.call_id == "22"
        shown = ui.detail.text(width=100)
        assert "output 199" in shown
        assert "Summary            exit 1" in shown
        assert "run_command · failed" in shown
        # Arguments come from JSON as labelled rows, commands as a code block.
        assert '{"command"' not in shown
        assert "command\npytest -q" in shown
        ui.tool = "write_plan"
        ui.refresh()
        assert not ui.visible
        assert "No matching" in ui.detail.text()
        ui.tool = "All"
        ui.query.text = "does not exist"
        assert not ui.visible


def test_run_scoping_retries_and_unfinished_states():
    archive = ToolArchive()
    call(archive, "same", failed=True, run_id="first")
    call(archive, "same", run_id="first")  # A retry is another inspectable attempt.
    call(archive, "same", run_id="second")
    archive.event(ToolStarted("run_command", "pending", "open", run_id="second"))
    archive.settle("interrupted")
    assert [c.state for c in archive.calls] == ["failed", "succeeded", "succeeded", "interrupted"]
    assert archive.calls[-1].elapsed is None
    archive.event(ToolStarted("read_file", "pending", "unknown"))
    archive.settle("unknown")
    assert archive.calls[-1].state == "unknown"


def test_payload_bounds_redaction_and_memory_eviction(monkeypatch):
    import pcode.inspection as inspection

    # Synthetic fixtures, not real credentials.
    text = capture({"password": "synthetic multi word value", "nested": {"api_key": "example"}})
    assert "synthetic" not in text and "example" not in text
    assert "[redacted]" in text
    assert "\x1b" not in capture("before\x1b[31mafter\x9b\u202e")
    assert "\x9b" not in capture("\x9b") and "\u202e" not in capture("\u202e")
    assert "truncated" in capture("x" * (PAYLOAD_LIMIT + 1))
    monkeypatch.setattr(inspection, "MEMORY_LIMIT", 100)
    archive = ToolArchive()
    call(archive, "one")
    assert "evicted" in archive.calls[0].result.read()
    assert len(archive.calls) == 1


def test_journal_lazy_payloads_old_sessions_torn_tail_and_cancellation(tmp_path):
    saved = SavedSession.create("test", tmp_path, tmp_path / "sessions")
    try:
        saved.append("turn_started", run_id="first", prompt="test")
        for i in range(45):
            saved.event(ToolStarted("read_file", "file", str(i), arguments='{"path":"file"}'))
            saved.event(ToolSummary("read_file", "done", call_id=str(i), result=f"result {i}"))
        saved.event(ToolSummary("old_tool", "old summary", call_id="old", error="old excerpt"))
        saved.event(ToolStarted("run_command", "running", "open"))
        saved.append("turn_cancelled", run_id="first")
        path = saved.directory / "transcript.jsonl"
        with path.open("ab") as file:
            file.write(b'{"torn":')
        archive = ToolArchive.load(path)
        assert len(archive.calls) == 47
        first = archive.calls[0]
        assert first.result.text is None and first.result.path == path
        assert first.result.read() == "result 0"
        assert first.arguments.read() == '{"path":"file"}'
        assert MISSING in archive.calls[-2].arguments.read()
        assert "old excerpt" in archive.calls[-2].result.read()
        assert archive.calls[-1].state == "interrupted"
        assert archive.calls[-1].elapsed is None
        path.unlink()
        assert "unavailable" in first.result.read()
    finally:
        saved.close()


def test_background_calls_link_without_marking_start_as_process_success():
    archive = ToolArchive()
    archive.event(ToolStarted("start_command", "server", "start", arguments="server"))
    archive.event(ToolSummary("start_command", "started", call_id="start", process_id="process"))
    archive.event(ToolStarted("check_command", "process", "check", process_id="process"))
    archive.event(ToolSummary("check_command", "exit 1", True, "check", process_id="process"))
    assert "Related calls: check" in archive.calls[0].details(archive.calls)
    assert archive.calls[1].state == "failed"


def test_inspector_keyboard_focus_scroll_filter_and_close(monkeypatch):
    import pcode.inspector_ui as inspector_ui

    monkeypatch.setattr(inspector_ui, "copy_to_clipboard", lambda text, output=None: (True, False))

    async def run():
        archive = ToolArchive()
        call(archive, "failed", failed=True)
        call(archive, "success")
        with create_pipe_input() as pipe:
            # A sized output so the pane renders; page scrolling needs render info.
            output = Vt100_Output(StringIO(), lambda: Size(rows=24, columns=80), enable_cpr=False)
            ui = ToolInspector(archive, input=pipe, output=output)
            task = asyncio.create_task(ui.run())
            await asyncio.sleep(0.05)
            assert ui.selected.call_id == "success"  # Newest first.
            # Arrows steer the list while the query keeps focus.
            pipe.send_text("/\x1b[B")
            await asyncio.sleep(0.05)
            assert ui.app.layout.has_focus(ui.query)
            assert ui.selected.call_id == "failed"
            pipe.send_text("\x1b[A\t")  # Up, then Tab to the list.
            await asyncio.sleep(0.05)
            assert ui.selected.call_id == "success"
            assert ui.app.layout.has_focus(ui.list)
            pipe.send_text("f")
            await asyncio.sleep(0.05)
            assert ui.selected.call_id == "failed"
            pipe.send_text("c")
            await asyncio.sleep(0.05)
            assert ui.notice == "Copied command"
            pipe.send_text("o")
            await asyncio.sleep(0.05)
            assert ui.notice == "Copied output"
            pipe.send_text("\t")
            await asyncio.sleep(0.05)
            assert ui.app.layout.has_focus(ui.detail)
            pipe.send_text("\x1b[6~")  # PageDown
            await asyncio.sleep(0.05)
            assert ui.detail.window.vertical_scroll > 0
            pipe.send_text("\x06")  # Ctrl+F
            await asyncio.sleep(0.05)
            assert ui.app.layout.has_focus(ui.query)
            pipe.send_text("missing")
            await asyncio.sleep(0.05)
            assert not ui.visible
            pipe.send_text("\x1b")
            await asyncio.wait_for(task, 2)

    asyncio.run(run())


def test_narrow_call_list_keeps_its_rows_beside_a_long_payload():
    """A long tool payload must not squeeze the stacked Calls pane to one row."""

    async def run():
        archive = ToolArchive()
        for identity in range(10):
            call(archive, str(identity))
        with create_pipe_input() as pipe:
            ui = ToolInspector(
                archive,
                input=pipe,
                output=Vt100_Output(
                    StringIO(), lambda: Size(rows=24, columns=80), enable_cpr=False
                ),
            )
            with set_app(ui.app):
                ui.app.renderer.render(ui.app, ui.app.layout)
                assert ui.list.window.render_info.window_height == 6
                assert ui.detail.window.render_info.window_height > 6

    asyncio.run(run())


def test_slash_commands_dispatch_and_complete():
    app = PreviewApp(console=Console(file=StringIO()))
    app.handle("/tools")
    assert app.inspector_requested == ""
    app.handle("/tools failed")
    assert app.inspector_requested == "failed"
    app.inspector_requested = None
    app.handle("/tools invalid")
    assert app.inspector_requested is None
    app.handle("/tools failed")
    assert app.inspector_requested == "failed"
    from pcode.commands import SlashCompleter

    completions = list(SlashCompleter(app.registry).get_completions(Document("/tools f"), None))
    assert [c.text for c in completions] == ["failed"]


def test_real_tool_capture_survives_resume_and_later_model_failure(tmp_path):
    requests = 0

    async def model(messages, info):
        nonlocal requests
        requests += 1
        if requests == 1:
            yield {0: DeltaToolCall(name="shell", json_args='{"command":"printf detail; exit 2"}')}
        else:
            raise RuntimeError("synthetic later failure")

    async def run():
        saved = SavedSession.create("test", tmp_path, tmp_path / "sessions")
        runtime = AgentRuntime(
            Agent(FunctionModel(stream_function=model), capabilities=[create_coder(tmp_path)]),
            saved,
        )
        try:
            try:
                _ = [e async for e in runtime.stream("test")]
            except RuntimeError:
                pass
            path = saved.directory / "transcript.jsonl"
            archive = ToolArchive.load(path)
            assert len(archive.calls) == 1
            entry = archive.calls[0]
            assert entry.state == "failed"
            assert "printf detail" in entry.arguments.read()
            assert "detail" in entry.result.read() and "· exit 2 ·" in entry.result.read()
            assert entry.run_id != "unavailable"
            identity, root = saved.info.id, saved.directory.parent
        finally:
            runtime.close()
        reopened = SavedSession.open(identity, root)
        resumed = AgentRuntime(runtime.agent, reopened)
        try:
            await resumed.restore()
            restored = ToolArchive.load(reopened.directory / "transcript.jsonl")
            assert restored.calls[0].details(restored.calls) == entry.details(archive.calls)
        finally:
            resumed.close()

    asyncio.run(run())


def test_unsaved_capture_survives_failure_and_new_resets(tmp_path):
    requests = 0

    async def model(messages, info):
        nonlocal requests
        requests += 1
        if requests == 1:
            yield {0: DeltaToolCall(name="shell", json_args='{"command":"printf unsaved"}')}
        else:
            raise RuntimeError("synthetic failure")

    async def run():
        runtime = AgentRuntime(
            Agent(FunctionModel(stream_function=model), capabilities=[create_coder(tmp_path)])
        )
        try:
            _ = [e async for e in runtime.stream("test")]
        except RuntimeError:
            pass
        assert len(runtime.inspections.calls) == 1
        assert "unsaved" in runtime.inspections.calls[0].result.read()
        runtime.reset()
        assert not runtime.inspections.calls

    asyncio.run(run())


def test_string_payload_secrets_are_redacted_before_persistence(tmp_path):
    # Deliberately non-key fixture text; no credential files are involved.
    content = (
        'password="synthetic multi word phrase"\n'
        "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----\n"
        "plain output remains"
    )
    projected = capture(content)
    assert "synthetic" not in projected and "multi word" not in projected
    assert "not-a-real-key" not in projected
    assert "plain output remains" in projected
    saved = SavedSession.create("test", tmp_path, tmp_path / "sessions")
    try:
        saved.event(ToolSummary("run_command", "done", call_id="one", result=projected))
        restored = ToolArchive.load(saved.directory / "transcript.jsonl")
        assert restored.calls[0].result.read() == projected
    finally:
        saved.close()


def test_index_skips_malformed_records_and_incrementally_reads_appends(tmp_path):
    import json

    path = tmp_path / "journal.jsonl"
    records = [
        {"kind": "ToolSummary"},
        {"kind": []},
        {"kind": "ToolSummary", "name": "tool", "call_id": []},
        {"kind": "ToolStarted", "name": "tool", "call_id": "one"},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    archive = ToolArchive.load(path)
    assert len(archive.calls) == 1 and archive.calls[0].state == "unknown"
    offset = archive._offset
    archive.update(path)
    assert archive._offset == offset and len(archive.calls) == 1
    with path.open("a") as file:
        file.write(
            json.dumps(
                {
                    "kind": "ToolSummary",
                    "name": "tool",
                    "call_id": "one",
                    "result": "appended output",
                    "elapsed_seconds": "invalid",
                }
            )
            + "\n"
        )
    archive.update(path)
    assert len(archive.calls) == 1
    assert archive.calls[0].state == "succeeded"
    assert archive.calls[0].elapsed is None
    assert archive.calls[0].result.read() == "appended output"
    # Truncation invalidates offsets and the metadata cache.
    path.write_text("[]\n")
    archive.update(path)
    assert not archive.calls


def test_execution_mode_is_recorded_for_command_tools_and_shown_in_both_panes():
    from pcode.tool_display import execution_mode

    assert execution_mode("shell", {"command": "sleep 5", "background": True}) == "background"
    assert execution_mode("shell", {"command": "ls"}) == "foreground"
    assert execution_mode("start_command", {}) == "background"
    assert execution_mode("run_command", {}) == "foreground"
    assert execution_mode("read_file", {"background": True}) == ""

    archive = ToolArchive()
    archive.event(ToolStarted("shell", "sleep 5", "job", command="sleep 5", execution="background"))
    # The summary carries no mode of its own and must not erase the started one.
    archive.event(ToolSummary("shell", "started", call_id="job", result="[j1 · running · 0s]"))
    waited = ToolStarted("shell", "ls", "wait", command="ls", execution="foreground")
    archive.event(waited)
    assert archive.calls[0].execution == "background"
    assert archive.calls[0].title().startswith("succeeded   shell (background) · ")
    assert "Execution: background" in archive.calls[0].details(archive.calls)
    # Foreground is the default, so it stays out of the row but is stated in details.
    assert "(background)" not in archive.calls[1].title()
    assert "Execution: foreground" in archive.calls[1].details(archive.calls)


def test_results_and_commands_share_the_same_block_rendering():
    from rich.syntax import Syntax

    from pcode.inspector_ui import arguments_renderables, result_renderables

    blocks = arguments_renderables('{"command": "ls -la"}', "ansi_dark")
    assert any(isinstance(block, Syntax) and block.lexer.name == "Bash" for block in blocks)
    plain, structured = (
        result_renderables("total 0\ndrwxr-xr-x", "ansi_dark"),
        result_renderables('{"ok": true}', "ansi_dark"),
    )
    assert isinstance(plain[0], Syntax) and plain[0].lexer.name == "Text only"
    assert isinstance(structured[0], Syntax) and structured[0].lexer.name == "JSON"


def test_details_break_commands_at_separators_but_copy_stays_verbatim():
    from pcode.inspector_ui import arguments_renderables, format_command

    chain = format_command("cd src && make test; echo done")
    assert chain == "cd src && \\\n  make test\necho done"
    assert format_command("a && b || c") == "a && \\\n  b || \\\n  c"
    assert format_command('echo "x; y && z"; ls') == 'echo "x; y && z"\nls'
    assert format_command("x=$(a; b) && c") == "x=$(a; b) && \\\n  c"
    assert format_command("case $x in a) ;; esac") == "case $x in a) ;; esac"
    assert format_command("already\nmulti; line") == "already\nmulti; line"
    blocks = arguments_renderables('{"command": "cd src && make test"}', "ansi_dark")
    assert blocks[-1].code == "cd src && \\\n  make test"


def test_copy_shortcuts_take_the_command_and_the_output(monkeypatch):
    from prompt_toolkit.input import create_pipe_input

    import pcode.inspector_ui as inspector_ui

    copied: list[str] = []
    monkeypatch.setattr(
        inspector_ui,
        "copy_to_clipboard",
        lambda text, output=None: (copied.append(text), (True, False))[1],
    )
    archive = ToolArchive()
    call(archive, "one")
    with create_pipe_input() as pipe:
        ui = ToolInspector(archive, input=pipe, output=DummyOutput())
        ui.copy("command")
        assert copied == ["pytest -q"] and ui.notice == "Copied command"
        ui.copy("output")
        assert copied[-1].endswith("output 199") and ui.notice == "Copied output"
        # Selecting another call drops the stale confirmation.
        archive.event(ToolStarted("read_file", "file", "two", arguments="not json"))
        ui.refresh()
        ui.list.buffer.cursor_position = ui.list.document.translate_row_col_to_index(0, 0)
        assert ui.selected.call_id == "two" and ui.notice == ""
        ui.copy("command")
        assert copied[-1] == "not json" and ui.notice == "Copied arguments"
        ui.selected = None
        ui.copy("output")
        assert ui.notice == "Nothing to copy"


def test_clipboard_prefers_a_helper_and_falls_back_to_osc52(monkeypatch):
    import base64

    from pcode import clipboard

    class Output:
        def __init__(self):
            self.raw = ""

        def write_raw(self, text):
            self.raw += text

        def flush(self):
            pass

    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setattr(clipboard.shutil, "which", lambda name: "/bin/" + name)
    runs = []
    monkeypatch.setattr(clipboard.subprocess, "run", lambda *args, **kwargs: runs.append(args))
    output = Output()
    assert clipboard.copy("ls -la", output) == (True, False)
    assert runs and output.raw == ""

    monkeypatch.setattr(clipboard.shutil, "which", lambda name: None)
    assert clipboard.copy("ls -la", output) == (True, False)
    assert base64.b64encode(b"ls -la").decode() in output.raw
    # No helper and no terminal to write to is a reported failure, not a crash.
    assert clipboard.copy("ls -la") == (False, False)
    copied, truncated = clipboard.copy("x" * (clipboard.LIMIT + 1), output)
    assert copied and truncated
    assert "truncated" in base64.b64decode(output.raw.rsplit(";", 1)[1][:-1]).decode()


def test_background_command_failure_classification():
    from pcode.tool_display import result_detail

    for name in ("check_command", "stop_command"):
        detail, failed = result_detail(
            name,
            {"command_id": "example"},
            "[stderr]\nfailed\n[status: finished]\n[exit code: 3]",
            "success",
        )
        assert failed and "exit 3" in detail
        _, failed = result_detail(name, {}, "[Error: unknown command ID 'example']", "success")
        assert failed
    _, failed = result_detail("check_command", {}, "(no output yet)\n[status: running]", "success")
    assert not failed
