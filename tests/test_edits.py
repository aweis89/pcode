import asyncio
from io import StringIO

import pytest
from pydantic_ai import ModelRetry
from rich.console import Console

from pcode.edits import MAX_SOURCE, completed_change
from pcode.filesystem import DisplayFileSystem, FileChangeEvent
from pcode.runtime import EditCompleted
from pcode.ui import Transcript


class Context:
    tool_call_id = "edit-1"

    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)

    @property
    def changes(self):
        return [event.change for event in self.events if isinstance(event, FileChangeEvent)]


def test_mutations_capture_actual_contents_and_failed_edits_emit_nothing(tmp_path):
    async def exercise():
        toolset = DisplayFileSystem(root_dir=tmp_path).get_toolset()
        ctx = Context()
        await toolset._write_file(ctx, "example.py", "context\r\nold\r\n")
        created = ctx.changes[-1]
        assert created.operation == "created" and created.added == 2
        assert "--- /dev/null" in created.patch
        await toolset._edit_file(ctx, "example.py", "old", "new")
        changed = ctx.changes[-1]
        assert " context" in changed.patch
        assert "-old" in changed.patch and "+new" in changed.patch
        assert (changed.added, changed.removed) == (1, 1)
        assert (tmp_path / "example.py").read_bytes() == b"context\r\nnew\r\n"
        await toolset._write_file(ctx, "example.py", "overwrite\n")
        assert "-new" in ctx.changes[-1].patch
        await toolset._edit_file(ctx, "example.py", "overwrite", "overwrite")
        assert ctx.changes[-1].operation == "unchanged"
        count = len(ctx.changes)
        for args in [("missing", "new", None), ("overwrite", "new", "stale")]:
            with pytest.raises(ModelRetry):
                await toolset._edit_file(ctx, "example.py", *args[:2], expected_hash=args[2])
        with pytest.raises(ModelRetry):
            await toolset._write_file(ctx, "example.py", "bad", expected_hash="stale")
        assert len(ctx.changes) == count
        assert (tmp_path / "example.py").read_text() == "overwrite\n"

    asyncio.run(exercise())


def test_binary_large_sensitive_and_missing_newline():
    assert completed_change("image", "\0", "text").omitted == "Binary content"
    assert completed_change("large", "x" * (MAX_SOURCE + 1), "small").omitted
    change = completed_change(".envrc", "SECRET=old", "SECRET=new")
    assert change.path == "[sensitive path]" and not change.patch
    assert r"\ No newline at end of file" in completed_change("x", "old", "new\n").patch
    change = completed_change("x", 'token = "old\nsecret"\n', 'token = "new\nsecret"\n')
    assert "old" not in change.patch and "new" not in change.patch
    assert "secret" not in change.patch


def test_completed_diffs_retained_while_hidden_and_reprojected():
    stream = StringIO()
    console = Console(file=stream, width=50)
    transcript = Transcript(console)
    change = completed_change("example.py", "old\n", "new\n")
    transcript.show_edits = False
    transcript.edit(change)
    assert not stream.getvalue()
    assert len(transcript.log.entries) == 1
    transcript.show_edits = True
    for objects, end, _ in transcript.replay():
        console.print(*objects, end=end)
    assert stream.getvalue().count("Edited example.py") == 1
    assert "+new" in stream.getvalue()
    assert len(transcript.log.entries) == 1
    transcript.show_edits = False
    assert transcript.replay() == []


def test_read_only_adapter_never_exposes_writes(tmp_path):
    original = DisplayFileSystem(root_dir=tmp_path, read_only=True)
    assert original.get_toolset().__class__.__name__ == "FilteredToolset"


def test_new_empty_file_is_not_an_unchanged_existing_file():
    assert completed_change("x", "", "", existed=False).operation == "created"
    assert completed_change("x", "", "").operation == "unchanged"


def test_completed_changes_survive_resume_without_rereading_files(tmp_path):
    import json

    from pydantic_ai import Agent
    from pydantic_ai.messages import ToolReturnPart
    from pydantic_ai.models.function import DeltaToolCall, FunctionModel

    from pcode.app import PreviewApp
    from pcode.live import AgentRuntime
    from pcode.sessions import SavedSession

    async def model(messages, info):
        if any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            yield "Done"
        else:
            yield {
                0: DeltaToolCall(
                    name="write_file",
                    json_args=json.dumps(
                        {
                            "path": "sample.py",
                            "content": "saved_change\n",
                        }
                    ),
                    tool_call_id="write-1",
                )
            }

    async def exercise():
        saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
        identity = saved.info.id
        agent = Agent(
            FunctionModel(stream_function=model), capabilities=[DisplayFileSystem(tmp_path)]
        )
        runtime = AgentRuntime(agent, saved)
        events = [event async for event in runtime.stream("write a file")]
        assert len([e for e in events if isinstance(e, EditCompleted)]) == 1
        runtime.close()
        (tmp_path / "sample.py").write_text("unrelated later contents\n")
        reopened = SavedSession.open(identity, tmp_path / "sessions")
        try:
            restored = AgentRuntime(agent, reopened)
            app = PreviewApp(model="test:local", runtime=restored, console=Console(file=StringIO()))
            app.transcript.show_edits = False
            app.replay()
            app.transcript.show_edits = True
            output = StringIO()
            console = Console(file=output, theme=app.transcript.rich_theme)
            for objects, end, _ in app.transcript.replay():
                console.print(*objects, end=end)
            assert "+saved_change" in output.getvalue()
            assert "unrelated later contents" not in output.getvalue()
            assert output.getvalue().count("Created sample.py") == 1
        finally:
            reopened.close()

    asyncio.run(exercise())


def test_write_only_destination_still_writes_without_a_diff(tmp_path):
    path = tmp_path / "write_only.py"
    path.write_text("old\n")
    path.chmod(0o200)

    async def exercise():
        ctx = Context()
        toolset = DisplayFileSystem(root_dir=tmp_path).get_toolset()
        await toolset._write_file(ctx, path.name, "new\n")
        assert ctx.changes[-1].omitted == "Before snapshot unavailable"
        assert not ctx.changes[-1].patch

    try:
        asyncio.run(exercise())
    finally:
        path.chmod(0o600)
    assert path.read_text() == "new\n"


def test_sensitive_symlink_target_is_not_captured(tmp_path):
    target = tmp_path / ".envrc"
    target.write_text("synthetic confidential contents\n")
    (tmp_path / "alias.py").symlink_to(target)

    async def exercise():
        ctx = Context()
        toolset = DisplayFileSystem(root_dir=tmp_path).get_toolset()
        await toolset._write_file(ctx, "alias.py", "new synthetic contents\n")
        assert ctx.changes[-1].path == "[sensitive path]"
        assert not ctx.changes[-1].patch

    asyncio.run(exercise())


def test_overwrite_non_utf8_content_and_missing_parent(tmp_path):
    path = tmp_path / "binary"
    path.write_bytes(b"\xff\xfe")

    async def exercise():
        ctx = Context()
        toolset = DisplayFileSystem(root_dir=tmp_path).get_toolset()
        await toolset._write_file(ctx, "binary", "text\n")
        assert ctx.changes[-1].omitted == "Binary or non-UTF-8 content"
        count = len(ctx.changes)
        with pytest.raises(ModelRetry):
            await toolset._write_file(ctx, "missing/child", "no")
        with pytest.raises(ModelRetry):
            await toolset._write_file(ctx, "../outside", "no")
        with pytest.raises(ModelRetry):
            await toolset._write_file(ctx, ".env", "no")
        assert len(ctx.changes) == count

    asyncio.run(exercise())
    assert path.read_text() == "text\n"


def test_parallel_writes_capture_their_own_operation(tmp_path):
    async def exercise():
        toolset = DisplayFileSystem(root_dir=tmp_path).get_toolset()
        contexts = [Context(), Context()]
        await asyncio.gather(
            toolset._write_file(contexts[0], "x.py", "first\n"),
            toolset._write_file(contexts[1], "x.py", "second\n"),
        )
        assert "+first" in contexts[0].changes[-1].patch
        assert "-first" in contexts[1].changes[-1].patch
        assert "+second" in contexts[1].changes[-1].patch

    asyncio.run(exercise())


def test_unquoted_credentials_are_redacted_before_persistence():
    change = completed_change("example.ini", "", "token = synthetic-value\npasswd = other-value\n")
    assert "synthetic-value" not in change.patch and "other-value" not in change.patch
    assert "[redacted]" in change.patch
