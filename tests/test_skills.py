import asyncio
from io import StringIO
from unittest.mock import patch

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.app import PreviewApp
from pcode.commands import SlashCompleter
from pcode.preferences import save_preferences
from pcode.runtime import Message, TextDelta
from pcode.skills import ASSET_ROOTS, discover_skills, skill_commands, skill_prompt
from pcode.ui import create_prompt


def write_skill(workspace, root, name, body="Do the thing.", frontmatter=True):
    path = workspace / root / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    header = f"---\nname: {name}\ndescription: Handles {name}.\n---\n" if frontmatter else ""
    path.write_text(header + body)
    return path


def make_app(workspace, **kwargs):
    return PreviewApp(console=Console(file=StringIO()), workspace=workspace, **kwargs)


def test_asset_roots_match_harness_inventory():
    """The local scan avoids importing Harness on the startup path; keep it honest."""
    from pydantic_ai_harness.repo_context import RepoContext

    assert tuple(RepoContext.asset_roots) == ASSET_ROOTS


def test_discovery_reads_only_frontmatter(tmp_path):
    write_skill(tmp_path, ".claude", "cache-report", body="Secret body")
    (skill,) = discover_skills(tmp_path)
    assert skill.name == "cache-report"
    assert skill.path == ".claude/skills/cache-report/SKILL.md"
    assert skill.description == "Handles cache-report."
    assert "Secret body" not in skill_prompt(skill, "")


def test_user_skill_dir_is_searched_by_default(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    path = home / ".agents" / "skills" / "review" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\ndescription: Handles review.\n---\n")
    workspace = tmp_path / "repo"
    workspace.mkdir()

    (skill,) = discover_skills(workspace)
    # Outside the workspace, the model needs the absolute path to read it.
    assert skill.path == str(path)
    assert skill.description == "Handles review."


def test_configured_dirs_replace_the_defaults(tmp_path):
    write_skill(tmp_path, ".agents", "ignored")
    extra = tmp_path / "team" / "skills" / "deploy"
    extra.mkdir(parents=True)
    (extra / "SKILL.md").write_text("Ship it.")
    save_preferences(skill_dirs="team/skills")

    skills = {skill.name: skill for skill in discover_skills(tmp_path)}
    # The asset roots are always scanned; only the configured list is replaced.
    assert sorted(skills) == ["deploy", "ignored"]
    assert skills["deploy"].path == "team/skills/deploy/SKILL.md"


def test_workspace_skills_shadow_user_skills(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    user = tmp_path / "home" / ".agents" / "skills" / "review"
    user.mkdir(parents=True)
    (user / "SKILL.md").write_text("User copy.")
    workspace = tmp_path / "repo"
    write_skill(workspace, ".claude", "review")

    (skill,) = discover_skills(workspace)
    assert skill.path == ".claude/skills/review/SKILL.md"


def test_empty_skill_dirs_keeps_only_the_asset_roots(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    user = tmp_path / "home" / ".agents" / "skills" / "review"
    user.mkdir(parents=True)
    (user / "SKILL.md").write_text("User copy.")
    save_preferences(skill_dirs="")

    assert discover_skills(tmp_path / "repo") == []


def test_discovery_without_frontmatter_and_across_roots(tmp_path):
    write_skill(tmp_path, ".claude", "shared", frontmatter=False)
    write_skill(tmp_path, ".agents", "shared")
    write_skill(tmp_path, ".agents", "other")
    skills = {skill.name: skill for skill in discover_skills(tmp_path)}
    assert sorted(skills) == ["other", "shared"]
    # Earlier roots win, so a duplicated name resolves the same way every launch.
    assert skills["shared"].path == ".claude/skills/shared/SKILL.md"
    assert skills["shared"].description == ""


def test_prompt_points_at_the_file_and_keeps_arguments(tmp_path):
    write_skill(tmp_path, ".claude", "review")
    (skill,) = discover_skills(tmp_path)
    assert skill_prompt(skill, "") == (
        'Use the "review" skill: read .claude/skills/review/SKILL.md '
        "and follow its instructions for this request."
    )
    assert skill_prompt(skill, "PR 12").endswith("\n\nPR 12")


@pytest.mark.parametrize(
    "style,expected,aliases",
    [
        ("prefix", "/skill:review", ()),
        ("bare", "/review", ()),
        ("both", "/skill:review", ("/review",)),
        ("off", None, ()),
    ],
)
def test_command_naming_styles(tmp_path, style, expected, aliases):
    write_skill(tmp_path, ".claude", "review")
    commands = skill_commands(discover_skills(tmp_path), lambda *_: None, style)
    if expected is None:
        assert commands == []
        return
    assert commands[0].name == expected
    assert commands[0].aliases == aliases


def test_registered_commands_queue_a_prompt(tmp_path):
    write_skill(tmp_path, ".claude", "review")
    app = make_app(tmp_path, model="test:model")
    assert app.skill_command_names == ["/skill:review"]
    assert app.registry.find("/skill:review").description == "Handles review."
    assert app.handle("/skill:review PR 12") is False
    assert app.skill_requested.endswith("\n\nPR 12")
    assert ".claude/skills/review/SKILL.md" in app.skill_requested


def test_skill_command_requires_a_model(tmp_path):
    write_skill(tmp_path, ".claude", "review")
    app = make_app(tmp_path)
    app.handle("/skill:review")
    assert app.skill_requested is None
    assert "requires a live model session" in app.transcript.console.file.getvalue()


def test_bare_style_never_shadows_a_builtin_command(tmp_path):
    write_skill(tmp_path, ".claude", "help")
    write_skill(tmp_path, ".claude", "review")
    save_preferences(skill_commands="both")
    app = make_app(tmp_path, model="test:model")
    assert app.registry.find("/help").description == "Commands and keyboard shortcuts"
    # The prefixed form still reaches a skill whose bare name was taken.
    assert app.registry.find("/skill:help") is not None
    assert app.registry.find("/review").name == "/skill:review"


def test_bare_style_drops_a_skill_named_like_a_command(tmp_path):
    write_skill(tmp_path, ".claude", "help")
    save_preferences(skill_commands="bare")
    app = make_app(tmp_path, model="test:model")
    assert app.skill_command_names == []
    assert app.registry.find("/help").description == "Commands and keyboard shortcuts"


def test_skill_commands_complete(tmp_path):
    write_skill(tmp_path, ".claude", "cache-report")
    app = make_app(tmp_path, model="test:model")
    completions = SlashCompleter(app.registry).get_completions(Document("/skill:"), CompleteEvent())
    assert [completion.text for completion in completions] == ["/skill:cache-report"]


def test_invoking_a_skill_command_sends_its_prompt(tmp_path):
    """The command path has to hand off to the prompt queue, like a typed message."""
    write_skill(tmp_path, ".claude", "review")

    async def run():
        calls = []
        answered = asyncio.Event()

        class Runtime:
            session = None
            recovery_blocked = ""

            async def stream(self, text):
                calls.append(text)
                yield TextDelta("ok")
                yield Message("ok")
                answered.set()

        app = PreviewApp(
            model="test:local",
            runtime=Runtime(),
            workspace=tmp_path,
            console=Console(file=StringIO(), color_system=None),
        )
        session = None

        with create_pipe_input() as pipe:

            def prompt(*args, **kwargs):
                nonlocal session
                session = create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)
                return session

            with patch("pcode.app.create_prompt", prompt):
                task = asyncio.create_task(app.run_async())
                try:
                    async with asyncio.timeout(5):
                        while session is None or not session.app.is_running:
                            await asyncio.sleep(0.01)
                        pipe.send_text("/skill:review PR 12\r")
                        await answered.wait()
                    pipe.send_text("/quit\r")
                    await asyncio.wait_for(task, 5)
                finally:
                    task.cancel()
        return calls

    (call,) = asyncio.run(run())
    assert call.startswith('Use the "review" skill: read .claude/skills/review/SKILL.md')
    assert call.endswith("\n\nPR 12")


def test_startup_summary_lists_skill_commands(tmp_path):
    write_skill(tmp_path, ".claude", "review")
    app = make_app(tmp_path, model="test:model")
    app.show_startup_context()
    assert "Skill commands: /skill:review" in app.transcript.console.file.getvalue()
