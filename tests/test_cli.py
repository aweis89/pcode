import sys
from unittest.mock import patch

import pytest
from pydantic_ai.exceptions import UserError
from pydantic_ai.providers.openai_codex import CredentialsRefreshError

from pcode.agent import create_agent
from pcode.app import main
from pcode.live import error_message


def test_cli_passes_model_and_workspace(monkeypatch, tmp_path):
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
    app.assert_called_once_with(
        theme="dark",
        model="openai-codex:gpt-5.6-luna",
        workspace=tmp_path,
    )
    app.return_value.run.assert_called_once()


def test_demo_never_initializes_a_provider(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pcode", "--demo", "-m", "invalid:provider"])
    with patch("pcode.agent.create_agent") as create:
        main()
    create.assert_not_called()
    assert "no model connected" in capsys.readouterr().out


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
