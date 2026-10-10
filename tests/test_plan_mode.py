import asyncio
from types import SimpleNamespace

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart

from pcode.extensions import plan_mode


class FakeAPI:
    def __init__(self, workspace):
        self.workspace = workspace
        self.is_worker = False
        self.notes = []
        self.ui = SimpleNamespace(notify=lambda text, level="info": self.notes.append(text))
        self.commands = {}
        self.tools = {}
        self.hooks = SimpleNamespace(
            on=SimpleNamespace(before_model_request=self._hook, before_tool_execute=self._guard)
        )

    def register_command(self, name, description, handler, **_):
        self.commands[name] = handler

    def tool(self, fn):
        self.tools[fn.__name__] = fn
        return fn

    def _guard(self, fn):
        self.guard = fn
        return fn

    def _hook(self, fn):
        self.hook = fn
        return fn


async def _remind(api, parts=None, agent="pcode"):
    request = ModelRequest(parts=parts or [UserPromptPart("hi")])
    ctx = SimpleNamespace(agent=SimpleNamespace(name=agent), messages=[request])
    await api.hook(ctx, SimpleNamespace(messages=[request]))
    return request.parts[len(parts or [None]) :]


def remind(api, parts=None, agent="pcode"):
    return [p.content for p in asyncio.run(_remind(api, parts, agent))]


def test_plan_mode_reminds_until_the_model_exits(tmp_path):
    api = FakeAPI(tmp_path)
    plan_mode.setup(api)
    assert remind(api) == []

    api.commands["/plan"]("Add Export Button!")
    assert (tmp_path / ".pcode/plans/.gitignore").exists()
    [note] = remind(api)
    assert ".pcode/plans/add-export-button.md" in note
    # Mid-turn (tool results), a retry resending it, and shared workers: nothing.
    assert remind(api, [ToolReturnPart("t", "ok", "id")]) == []
    assert remind(api, [UserPromptPart("hi"), UserPromptPart(note)]) == []
    assert remind(api, agent="worker") == []
    tool_def = SimpleNamespace(name="exit_plan_mode")
    worker = SimpleNamespace(agent=SimpleNamespace(name="worker"))
    with pytest.raises(ModelRetry):
        asyncio.run(api.guard(worker, call=None, tool_def=tool_def, args={}))

    # Survives a reload: a fresh setup reads the marker.
    reloaded = FakeAPI(tmp_path)
    plan_mode.setup(reloaded)
    assert "add-export-button.md" in reloaded.tools["exit_plan_mode"]()
    assert remind(reloaded) == []
    assert "not on" in reloaded.tools["exit_plan_mode"]()


def test_bare_plan_resumes_latest_and_off_stops(tmp_path):
    api = FakeAPI(tmp_path)
    plan_mode.setup(api)
    (tmp_path / ".pcode/plans").mkdir(parents=True)
    (tmp_path / ".pcode/plans/old-idea.md").write_text("# plan\n")
    api.commands["/plan"]("")
    assert "Resuming .pcode/plans/old-idea.md" in api.notes[-1]
    api.commands["/plan"]("off")
    assert remind(api) == []


def test_workers_get_nothing(tmp_path):
    api = FakeAPI(tmp_path)
    api.is_worker = True
    plan_mode.setup(api)
    assert not api.commands and not api.tools
