import json

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    ToolCallPart,
    ToolCallPartDelta,
    ToolReturnPart,
)
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from pcode.agent import create_coder
from pcode.code_mode import SANDBOXED_TOOLS
from pcode.edit_preview import StreamingEditPreview
from pcode.runtime import EditPreview
from pcode.tool_display import label, result_detail, target


@pytest.fixture
def preferences(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("EXA_API_KEY", raising=False)

    def write(**values):
        path = tmp_path / "config" / "pcode"
        path.mkdir(parents=True, exist_ok=True)
        (path / "preferences.json").write_text(json.dumps(values))

    return write


def tool_names(workspace) -> set[str]:
    seen: set[str] = set()

    async def model(messages, info):
        seen.update(tool.name for tool in info.function_tools)
        yield "Done"

    Agent(FunctionModel(stream_function=model), capabilities=[create_coder(workspace)]).run_sync(
        "inspect"
    )
    return seen


def test_code_mode_is_off_by_default(preferences, tmp_path):
    preferences()
    names = tool_names(tmp_path)
    assert "run_code" not in names
    assert {"read_file", "grep", "list_files"} <= names


def test_enabling_code_mode_sandboxes_only_read_only_tools(preferences, tmp_path):
    preferences(code_mode="on")
    names = tool_names(tmp_path)
    assert "run_code" in names
    assert not set(SANDBOXED_TOOLS) & names
    # Tools whose terminal display is the point keep issuing their own calls.
    assert {"edit_file", "write_file", "shell", "write_plan", "delegate_task"} <= names


def test_sandboxed_code_calls_the_real_file_tools(preferences, tmp_path):
    preferences(code_mode="on")
    (tmp_path / "one.txt").write_text("alpha\n")
    (tmp_path / "two.txt").write_text("beta\n")
    calls = 0

    async def model(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {
                0: DeltaToolCall(
                    name="run_code",
                    json_args=json.dumps(
                        {
                            "code": (
                                "import asyncio\n"
                                "one, two = await asyncio.gather(\n"
                                "    read_file(path='one.txt'),\n"
                                "    read_file(path='two.txt'),\n"
                                ")\n"
                                "[one, two]\n"
                            )
                        }
                    ),
                )
            }
        else:
            returned = next(
                part
                for message in messages
                for part in message.parts
                if isinstance(part, ToolReturnPart)
            )
            assert returned.tool_name == "run_code"
            text = json.dumps(returned.content)
            assert "alpha" in text and "beta" in text
            yield "Done"

    Agent(FunctionModel(stream_function=model), capabilities=[create_coder(tmp_path)]).run_sync(
        "read both files"
    )
    assert calls == 2


def test_snippets_display_the_tool_calls_they_make():
    code = (
        "import asyncio\n\n"
        "# grep('not a call')\n"
        "hits = await grep(pattern='TODO')\n"
        "files = await asyncio.gather(read_file(path='a'), read_file(path='b'))\n"
        "len(files)\n"
    )
    assert label("run_code") == "Code"
    assert target("run_code", {"code": code}) == "grep · read_file ×2 · 5 lines"
    assert target("run_code", {"code": "1 + 1"}) == "1 line"
    # A snippet that does not parse still gets an honest size, not a wrong summary.
    assert target("run_code", {"code": "read_file(path="}) == "1 line"
    assert target("run_code", {}) == "code unavailable"


def test_streaming_snippets_preview_only_complete_lines():
    preview = StreamingEditPreview()
    start = PartStartEvent(index=0, part=ToolCallPart("run_code", '{"code":"', tool_call_id="a"))
    assert preview.update(start) == []
    preview.updated.clear()
    streamed = preview.update(
        PartDeltaEvent(
            index=0,
            delta=ToolCallPartDelta(args_delta="await grep(pattern=\\u0027TODO\\u0027)\\nlen("),
        )
    )
    assert streamed[-1] == EditPreview(
        "edit-preview:0", "run_code", "await grep(pattern='TODO')\n", kind="code"
    )
    # An unterminated line only appears once the model has finished writing it.
    assert "len(" not in streamed[-1].text
    part = ToolCallPart(
        "run_code", {"code": "await grep(pattern='TODO')\nlen(hits)"}, tool_call_id="a"
    )
    assert preview.update(PartEndEvent(index=0, part=part))[-1].text.endswith("len(hits)")
    # The snippet leaves the box the moment it is dispatched for execution.
    assert preview.update(FunctionToolCallEvent(part)) == [EditPreview("edit-preview:0")]


def test_failed_snippets_report_the_sandbox_error():
    detail, failed = result_detail(
        "run_code", {"code": "read_file(path='gone')"}, "No such file: gone", "retry"
    )
    assert failed
    assert detail.startswith("read_file · 1 line → Retry requested")
