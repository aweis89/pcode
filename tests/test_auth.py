"""Environment auth and login dispatch without accessing real credentials."""

from io import StringIO
from unittest.mock import Mock

import pytest
from rich.console import Console

from pcode.agent import create_agent
from pcode.app import PreviewApp


@pytest.fixture(autouse=True)
def isolated_credentials(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("PCODE_LLM_PROXY", raising=False)
    monkeypatch.delenv("PCODE_ANTHROPIC_AUTH", raising=False)


def test_anthropic_can_start_without_key(tmp_path):
    app = PreviewApp(model="anthropic:test-model", workspace=tmp_path)
    assert app.handle("/login") is False
    assert app.login_requested is True
    app.runtime.close()


def test_environment_key_used(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-test-key")
    factory = Mock(return_value="test")
    monkeypatch.setattr("pcode.auth.anthropic_model", factory)
    create_agent("anthropic:test-model", tmp_path)
    factory.assert_called_once_with("anthropic:test-model", "synthetic-test-key")


def test_other_provider_login_does_not_request_pi():
    buffer = StringIO()
    app = PreviewApp(model="test", runtime=Mock(), console=Console(file=buffer))
    app.handle("/login")
    assert not app.login_requested
    assert "Anthropic only" in buffer.getvalue()


def test_preview_login_requests_pi():
    app = PreviewApp(console=Console(file=StringIO()))
    app.handle("/login")
    assert app.login_requested is True
