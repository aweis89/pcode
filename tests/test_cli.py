import io
import sys
from unittest.mock import patch

import pytest
from pydantic_ai.exceptions import UserError
from pydantic_ai.providers.openai_codex import CredentialsRefreshError

from pcode.agent import create_agent
from pcode.app import main
from pcode.live import error_message
from pcode.sessions import SavedSession, list_sessions


def test_cli_passes_model_and_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("PCODE_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pcode",
            "-m",
            "openai-codex:gpt-5.6-luna",
            "-C",
            str(tmp_path),
        ],
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    with patch("pcode.app.PreviewApp") as app:
        main()
    assert app.call_args.kwargs["theme"] == "dark"
    assert app.call_args.kwargs["color_style"] == "palette"
    assert app.call_args.kwargs["model"] == "openai-codex:gpt-5.6-luna"
    assert app.call_args.kwargs["workspace"] == tmp_path
    assert app.call_args.kwargs["saved_session"] is None
    assert app.call_args.kwargs["save"] is True
    assert not (tmp_path / "sessions").exists()
    app.return_value.run.assert_called_once()


def test_resume_restores_model_workspace_and_releases_lock(monkeypatch, tmp_path):
    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    identity = saved.info.id
    saved.close()
    monkeypatch.setattr(sys, "argv", ["pcode", "--resume", identity, "--session-dir", str(root)])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    with patch("pcode.app.PreviewApp") as app:
        main()
    assert app.call_args.kwargs["resume"] is True
    assert app.call_args.kwargs["model"] == "test:local"
    assert app.call_args.kwargs["workspace"] == tmp_path
    reopened = SavedSession.open(identity, root)
    reopened.close()


def test_no_save_never_creates_session(monkeypatch, tmp_path):
    root = tmp_path / "sessions"
    monkeypatch.setenv("PCODE_SESSION_DIR", str(root))
    monkeypatch.setattr(sys, "argv", ["pcode", "-m", "test:local", "--no-save"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    with patch("pcode.app.PreviewApp") as app:
        main()
    assert app.call_args.kwargs["saved_session"] is None
    assert list_sessions(root) == []


def test_resume_rejects_different_workspace(monkeypatch, tmp_path, capsys):
    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    saved.close()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pcode",
            "--resume",
            "latest",
            "--session-dir",
            str(root),
            "-C",
            str(tmp_path / "elsewhere"),
        ],
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2
    assert "cross-repo resume" in capsys.readouterr().err


def test_demo_never_initializes_a_provider(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pcode", "--demo", "-m", "invalid:provider"])
    with patch("pcode.agent.create_agent") as create:
        main()
    create.assert_not_called()
    assert "no model connected" in capsys.readouterr().out


def test_cli_passes_initial_prompt_to_interactive_app(monkeypatch, tmp_path):
    monkeypatch.setenv("PCODE_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(sys, "argv", ["pcode", "-m", "test:local", "fix the failing test"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    with patch("pcode.app.PreviewApp") as app:
        main()
    assert app.call_args.kwargs["initial_prompt"] == "fix the failing test"
    assert app.call_args.kwargs["console"] is None
    app.return_value.run.assert_called_once()
    app.return_value.run_print.assert_not_called()


def test_print_runs_without_a_terminal_and_exits_with_turn_status(monkeypatch, tmp_path):
    monkeypatch.setenv("PCODE_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(sys, "argv", ["pcode", "-m", "test:local", "--print", "summarize"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    with patch("pcode.app.PreviewApp") as app:
        app.return_value.run_print.return_value = True
        main()
    assert app.call_args.kwargs["initial_prompt"] == "summarize"
    assert app.call_args.kwargs["console"].stderr is True
    app.return_value.run_print.assert_called_once_with("summarize")
    app.return_value.run.assert_not_called()
    with patch("pcode.app.PreviewApp") as app:
        app.return_value.run_print.return_value = False
        with pytest.raises(SystemExit) as raised:
            main()
    assert raised.value.code == 1


def test_print_reads_prompt_from_stdin_when_omitted(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PCODE_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(sys, "argv", ["pcode", "-m", "test:local", "-p"])
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(sys, "stdin", io.StringIO("piped question\n"))
    with patch("pcode.app.PreviewApp") as app:
        app.return_value.run_print.return_value = True
        main()
    app.return_value.run_print.assert_called_once_with("piped question\n")
    monkeypatch.setattr(sys, "stdin", io.StringIO("   \n"))
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2
    assert "non-empty prompt" in capsys.readouterr().err


def test_print_without_prompt_on_a_terminal_is_an_error(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pcode", "-m", "test:local", "-p"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2
    assert "PROMPT argument or text on stdin" in capsys.readouterr().err


def test_invalidated_login_shows_safe_error_code_only():
    error = CredentialsRefreshError("HTTP 401: refresh_token_invalidated; sensitive-body-sentinel")
    message = error_message(error)
    assert "refresh_token_invalidated" in message
    assert "codex login" in message
    assert "sensitive-body-sentinel" not in message


def test_missing_login_has_actionable_message_without_dumping_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    with pytest.raises(UserError) as raised:
        create_agent("openai-codex:gpt-5.6-luna", tmp_path)
    assert error_message(raised.value) == (
        "Provider login missing or invalid. Run `codex login`, then restart pcode."
    )


@pytest.mark.parametrize("color_style", ["palette", "terminal"])
def test_demo_cli_passes_color_style(monkeypatch, color_style):
    monkeypatch.setattr(
        sys, "argv", ["pcode", "--demo", "--theme", "light", "--color-style", color_style]
    )
    with patch("pcode.app.PreviewApp") as app:
        main()
    app.assert_called_once_with(theme="light", color_style=color_style)
    app.return_value.transcript.events.assert_called_once()
