import asyncio
from unittest.mock import patch

from pydantic_ai.models.function import FunctionModel

from pcode.agent import create_agent, create_coder
from pcode.repo_context import AutomaticRepoContext


def test_inventory_is_metadata_only_and_preserves_instructions(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Repository development guidance")
    skill = tmp_path / ".claude/skills/example/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("Asset body should not be loaded")
    capability = AutomaticRepoContext(workspace_dir=tmp_path)
    instructions = capability.get_instructions()
    assert "Repository development guidance" in instructions
    assert ".claude/skills/example/SKILL.md" in instructions
    assert "Asset body should not be loaded" not in instructions
    assert "Call `inventory_agent_context`" not in instructions
    assert capability.get_toolset() is None


def test_inventory_cached_within_run_and_refreshed_between_runs(tmp_path):
    async def run():
        capability = AutomaticRepoContext(workspace_dir=tmp_path)
        first = await capability.for_run(None)
        assert "No assistant configuration directories" in first.get_instructions()
        (tmp_path / ".agents").mkdir()
        assert "No assistant configuration directories" in first.get_instructions()
        second = await first.for_run(None)
        assert '"root":".agents"' in second.get_instructions()

    asyncio.run(run())


def test_main_agent_receives_inventory_without_tool_call(tmp_path, monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    (tmp_path / ".claude").mkdir()
    seen = []

    async def respond(messages, info):
        seen.append(info)
        assert "inventory_agent_context" not in {tool.name for tool in info.function_tools}
        assert '"root":".claude"' in info.instructions
        assert "Call `inventory_agent_context`" not in info.instructions
        yield "Done"

    agent = create_agent("test", tmp_path)
    agent.run_sync("Hello", model=FunctionModel(stream_function=respond))
    assert len(seen) == 1


def test_explorer_uses_automatic_context(tmp_path, monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    # Capture construction to check the actual explorer configuration.
    from pydantic_ai import Agent

    with patch("pcode.agent.Agent", wraps=Agent) as constructor:
        create_coder(tmp_path)
    capabilities = constructor.call_args.kwargs["capabilities"]
    context = next(cap for cap in capabilities if isinstance(cap, AutomaticRepoContext))
    assert context.get_toolset() is None
    assert "inventory_agent_context" not in context.get_instructions()


def test_startup_summary_reports_only_selected_instructions_and_metadata(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("selected instruction body")
    (tmp_path / "AGENTS.md").write_text("selected instruction body")
    root = tmp_path / ".claude"
    (root / "agents").mkdir(parents=True)
    (root / "agents/helper.md").write_text("agent body")
    (root / "settings.json").write_text('{"hooks": {}}')
    capability = AutomaticRepoContext(workspace_dir=tmp_path)
    # Instruction reads are allowed; asset bodies must never be read.
    from pathlib import Path

    original = Path.read_text

    def read(path, *args, **kwargs):
        assert path.name in {"CLAUDE.md", "AGENTS.md"}
        return original(path, *args, **kwargs)

    with patch.object(Path, "read_text", read):
        text = "\n".join(capability.startup_summary())
    assert "Loaded repository instructions: CLAUDE.md" in text
    assert "AGENTS.md" not in text
    assert "selected instruction body" not in text
    assert "Agent: .claude/agents/helper.md" in text
    assert "Settings/hooks: .claude/settings.json" in text
    assert "contents not loaded, hooks not run" in text
    assert "Repository context is refreshed" not in text


def test_startup_summary_empty_repository(tmp_path):
    summary = AutomaticRepoContext(workspace_dir=tmp_path).startup_summary()
    assert summary == []


def test_startup_summary_instructions_only(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Repository guidance")
    summary = AutomaticRepoContext(workspace_dir=tmp_path).startup_summary()
    assert summary == ["Loaded repository instructions: AGENTS.md"]


def test_startup_summary_configuration_only(tmp_path):
    (tmp_path / ".claude").mkdir()
    summary = AutomaticRepoContext(workspace_dir=tmp_path).startup_summary()
    assert summary == [
        "Discovered configuration (paths only; contents not loaded, hooks not run):",
        "  .claude/",
    ]


def test_app_displays_actual_agent_context_and_preview_stays_local(tmp_path, monkeypatch):
    from io import StringIO

    from rich.console import Console

    from pcode.app import PreviewApp
    from pcode.live import AgentRuntime

    monkeypatch.delenv("EXA_API_KEY", raising=False)
    (tmp_path / "AGENTS.md").write_text("do not display this body")
    output = StringIO()
    app = PreviewApp(
        model="test",
        workspace=tmp_path,
        console=Console(file=output, width=160, color_system=None),
        runtime=AgentRuntime(create_agent("test", tmp_path)),
    )
    app.show_startup_context()
    assert "Loaded repository instructions: AGENTS.md" in output.getvalue()
    assert "do not display this body" not in output.getvalue()
    output.seek(0)
    output.truncate()
    preview = PreviewApp(console=Console(file=output), workspace=tmp_path)
    preview.show_startup_context()
    assert output.getvalue() == ""
