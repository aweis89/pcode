"""Environment auth and login dispatch without accessing real credentials."""

import asyncio
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from rich.console import Console

from pcode.agent import create_agent
from pcode.app import PreviewApp


@pytest.fixture(autouse=True)
def isolated_credentials(monkeypatch):
    # conftest redirects XDG_CONFIG_HOME, so no stored sign-in is visible here.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("PCODE_LLM_PROXY", raising=False)


def test_anthropic_can_start_without_key(tmp_path):
    app = PreviewApp(model="anthropic:test-model", workspace=tmp_path)
    asyncio.run(app._initialize_runtime())
    assert isinstance(app.runtime.agent.model, str)
    app.runtime.close()


@pytest.mark.parametrize(
    "name, fable_5_2",
    [
        ("claude-fable-5-2", True),
        ("claude-mythos-5-2", True),
        ("claude-fable-5-2-20261109", True),
        ("claude-fable-5", False),
    ],
)
def test_fable_5_2_profile_binds_thinking_and_refuses_forced_tools(name, fable_5_2):
    # 5.2 rejects a forced tool choice with a 400, and without the binding flag
    # an edited prefix fails the turn instead of being retried with `drop_block`.
    # Pydantic AI's own profile covers this since 2.69; this guards a regression.
    from pcode.auth import anthropic_model

    profile = anthropic_model(f"anthropic:{name}", "synthetic-test-key").profile
    assert profile["anthropic_binds_thinking_blocks"] is fable_5_2
    assert profile["supports_forced_tool_choice"] is not fable_5_2
    assert profile["anthropic_supports_adaptive_thinking"] is True


def test_environment_key_used(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-test-key")
    factory = Mock(return_value="test")
    monkeypatch.setattr("pcode.auth.anthropic_model", factory)
    create_agent("anthropic:test-model", tmp_path)
    factory.assert_called_once_with("anthropic:test-model", "synthetic-test-key")


def test_login_works_from_a_session_on_another_provider():
    buffer = StringIO()
    app = PreviewApp(model="openai-codex:test", runtime=Mock(), console=Console(file=buffer))
    app.handle("/login claude")
    assert app.controller.login_requested == "claude"


def test_login_signs_in_to_claude_code():
    buffer = StringIO()
    app = PreviewApp(console=Console(file=buffer))
    app.handle("/login")
    assert app.controller.login_requested == "claude"
    app.controller.login_requested = None
    app.handle("/login anthropic")
    assert app.controller.login_requested is None
    assert "Usage: /login [claude|openai-codex|antigravity]" in buffer.getvalue()


def test_bare_login_defaults_to_current_provider():
    for model, expected in (
        ("openai-codex:gpt-5", "openai-codex"),
        ("antigravity:gemini-4-neon-high", "antigravity"),
        ("claude:claude-sonnet-5", "claude"),
        ("anthropic:claude-sonnet-5", "claude"),
    ):
        runtime = SimpleNamespace(
            agent=SimpleNamespace(model=None, model_settings=None), history=[]
        )
        app = PreviewApp(model=model, runtime=runtime, console=Console(file=StringIO()))
        app.handle("/login")
        assert app.controller.login_requested == expected


def test_unknown_login_source_is_rejected():
    buffer = StringIO()
    app = PreviewApp(console=Console(file=buffer))
    app.handle("/login codex")
    assert app.controller.login_requested is None
    assert "Usage: /login" in buffer.getvalue()
