"""Model suggestions inspect configuration, never real credential contents."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from pcode.models import active_providers, model_catalog


@pytest.fixture(autouse=True)
def isolated_providers(monkeypatch, tmp_path):
    for name in ("PCODE_ANTHROPIC_AUTH", "ANTHROPIC_API_KEY", "PCODE_LLM_PROXY", "CODEX_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("PCODE_MERIDIAN_BASE_URL", raising=False)
    monkeypatch.setattr("pcode.models.shutil.which", lambda _: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


def test_no_configuration():
    assert active_providers(None) == set()


def test_current_provider_stays_visible():
    assert active_providers("anthropic:custom-id") == {"anthropic"}
    assert active_providers("openai-codex:custom-id") == {"openai-codex"}
    assert active_providers("test:local") == set()


@pytest.mark.parametrize("source", ["api-key", "pi"])
def test_anthropic_configuration(monkeypatch, source):
    monkeypatch.setenv("PCODE_ANTHROPIC_AUTH", source)
    if source == "api-key":
        monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-key")
    assert active_providers(None) == {"anthropic"}


def test_codex_presence_not_contents(monkeypatch, tmp_path):
    directory = tmp_path / "custom-codex"
    directory.mkdir()
    (directory / "auth.json").write_text("synthetic-not-even-json")
    monkeypatch.setenv("CODEX_HOME", str(directory))
    monkeypatch.setattr(Path, "read_text", Mock(side_effect=AssertionError("must not read")))
    assert active_providers("anthropic:custom") == {"anthropic", "openai-codex"}


def test_proxy_limits_providers(monkeypatch):
    monkeypatch.setenv("PCODE_LLM_PROXY", "http://localhost:8080")
    monkeypatch.setenv("PCODE_ANTHROPIC_AUTH", "pi")
    assert active_providers("openai-codex:custom") == {"openai-codex"}


def test_catalog_uses_installed_sdk_and_keeps_custom_current():
    models = model_catalog({"anthropic", "openai-codex"}, "anthropic:custom-id")
    assert models[0] == "anthropic:custom-id"
    assert "anthropic:claude-opus-5" in models
    assert "openai-codex:gpt-5.6-luna" in models
    assert len(models) == len(set(models))
    assert all(name.startswith(("anthropic:", "openai-codex:")) for name in models)
    assert all("chat-latest" not in name for name in models)
    assert model_catalog(set()) == []
    assert all(name.startswith("anthropic:") for name in model_catalog({"anthropic"}))
