import asyncio
import sys
from io import StringIO
from types import SimpleNamespace

import pytest
from rich.console import Console

from pcode import codex_login
from pcode.auth import LoginError


def fake_codex(monkeypatch, script: str) -> None:
    """Stand in for the `codex` binary with a Python script."""
    monkeypatch.setattr(codex_login, "codex_executable", lambda: sys.executable)
    original = asyncio.create_subprocess_exec

    async def spawn(executable, *arguments, **kwargs):
        return await original(executable, "-c", script, *arguments, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)


def test_missing_cli_is_a_login_error(monkeypatch):
    monkeypatch.setattr(codex_login.shutil, "which", lambda name: None)
    with pytest.raises(LoginError, match="`codex` command was not found"):
        codex_login.codex_executable()


def test_login_relays_output_lines(monkeypatch):
    fake_codex(
        monkeypatch,
        "import sys; assert sys.argv[1:] == ['login']; "
        "print('If your browser did not open, navigate to this URL:'); print(); "
        "print('https://auth.openai.com/oauth/authorize?state=synthetic')",
    )
    lines = []
    asyncio.run(codex_login.login(notify=lines.append))
    assert lines == [
        "If your browser did not open, navigate to this URL:",
        "https://auth.openai.com/oauth/authorize?state=synthetic",
    ]


def test_failed_login_reports_the_exit_status(monkeypatch):
    fake_codex(monkeypatch, "import sys; sys.exit(3)")
    with pytest.raises(LoginError, match="exited with status 3"):
        asyncio.run(codex_login.login())


def test_login_times_out_and_kills_the_cli(monkeypatch):
    fake_codex(monkeypatch, "import time; time.sleep(30)")
    with pytest.raises(LoginError, match="timed out"):
        asyncio.run(codex_login.login(timeout=0.2))


def test_logout_runs_the_cli(monkeypatch):
    fake_codex(monkeypatch, "import sys; assert sys.argv[1:] == ['logout']")
    asyncio.run(codex_login.logout())


def make_app(model: str):
    from pcode.app import PreviewApp

    runtime = SimpleNamespace(agent=SimpleNamespace(model="original"), history=["existing"])
    buffer = StringIO()
    app = PreviewApp(model=model, runtime=runtime, console=Console(file=buffer))
    return app, runtime, buffer


def test_login_command_rebuilds_a_codex_conversation_model(monkeypatch):
    app, runtime, buffer = make_app("openai-codex:test-model")
    calls = []

    async def fake_login(**kwargs):
        kwargs["notify"]("https://auth.openai.com/oauth/authorize?state=synthetic")
        calls.append("login")

    monkeypatch.setattr(codex_login, "login", fake_login)
    monkeypatch.setattr("pcode.agent.codex_model", lambda model: f"rebuilt {model}")
    app.handle("/login openai-codex")
    assert app.login_requested == "openai-codex"
    asyncio.run(app.perform_login())

    assert app.login_requested is None
    assert calls == ["login"]
    assert runtime.agent.model == "rebuilt openai-codex:test-model"
    assert runtime.history == ["existing"]
    output = buffer.getvalue()
    assert "auth.openai.com" in output and "Signed in to OpenAI Codex" in output


def test_login_keeps_a_non_codex_conversation_model(monkeypatch):
    app, runtime, _ = make_app("anthropic:test-model")

    async def fake_login(**kwargs):
        pass

    monkeypatch.setattr(codex_login, "login", fake_login)
    app.handle("/login openai-codex")
    asyncio.run(app.perform_login())
    assert runtime.agent.model == "original"


def test_failed_login_reports_without_touching_the_model(monkeypatch):
    app, runtime, buffer = make_app("openai-codex:test-model")

    async def fail(**kwargs):
        raise LoginError("The `codex` command was not found.")

    monkeypatch.setattr(codex_login, "login", fail)
    app.handle("/login openai-codex")
    asyncio.run(app.perform_login())
    assert runtime.agent.model == "original"
    assert "`codex` command was not found" in buffer.getvalue()


def test_unknown_login_source_shows_usage():
    app, _, buffer = make_app("openai-codex:test-model")
    app.handle("/login gemini")
    assert app.login_requested is None
    assert "Usage: /login [anthropic|openai-codex]" in buffer.getvalue()


def test_logout_command_runs_codex_logout(monkeypatch):
    app, _, buffer = make_app("openai-codex:test-model")
    calls = []

    async def fake_logout(**kwargs):
        calls.append("logout")

    monkeypatch.setattr(codex_login, "logout", fake_logout)
    app.handle("/logout openai-codex")
    assert app.logout_requested == "openai-codex"
    asyncio.run(app.perform_logout())
    assert app.logout_requested is None
    assert calls == ["logout"]
    assert "Removed the Codex CLI's stored login" in buffer.getvalue()
