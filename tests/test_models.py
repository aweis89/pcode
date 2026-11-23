"""Model suggestions inspect configuration, never real credential contents."""

from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic_ai.providers import infer_provider_class

from pcode.models import (
    ENV_PROVIDERS,
    PROVIDERS,
    active_providers,
    latest_models,
    model_catalog,
    model_sort_key,
)


@pytest.fixture(autouse=True)
def isolated_providers(monkeypatch, tmp_path):
    for name in (
        "ANTHROPIC_API_KEY",
        "PCODE_LLM_PROXY",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    for requirements in ENV_PROVIDERS.values():
        for group in requirements:
            for name in group:
                monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("pcode.models.shutil.which", lambda _: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


@pytest.mark.parametrize("allowed", ["openai-codex,anthropic", " openai-codex , anthropic "])
def test_provider_filter_matches_exact_names(monkeypatch, tmp_path, allowed):
    from pcode.preferences import save_preferences

    monkeypatch.setenv("PCODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-key")
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").touch()
    save_preferences(model_providers=allowed)
    providers = active_providers("openai:custom")
    assert providers == {"openai-codex", "anthropic"}
    assert {
        name.partition(":")[0] for name in model_catalog(providers, "openai:custom")
    } == providers


def test_provider_filter_does_not_activate_providers(monkeypatch, tmp_path):
    from pcode.preferences import save_preferences

    monkeypatch.setenv("PCODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    save_preferences(model_providers="anthropic")
    assert active_providers("openai:custom") == set()
    save_preferences(model_providers="")
    assert active_providers("openai:custom") == {"openai"}


@pytest.mark.parametrize(
    "value", ["openai-code", "anthropic,,openai", "anthropic,", "openai codex"]
)
def test_provider_filter_rejects_invalid_names(value):
    from pcode.preferences import SETTINGS

    with pytest.raises(ValueError):
        SETTINGS["model_providers"].validate("model_providers", value)


def test_no_configuration():
    assert active_providers(None) == set()


def test_current_provider_stays_visible():
    assert active_providers("anthropic:custom-id") == {"anthropic"}
    assert active_providers("openai-codex:custom-id") == {"openai-codex"}
    assert active_providers("test:local") == set()


def test_anthropic_configuration(monkeypatch):
    assert active_providers(None) == set()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-key")
    assert active_providers(None) == {"anthropic"}


def test_codex_presence_not_contents(monkeypatch, tmp_path):
    directory = tmp_path / "custom-codex"
    directory.mkdir()
    auth = directory / "auth.json"
    auth.write_text("synthetic-not-even-json")
    monkeypatch.setenv("CODEX_HOME", str(directory))
    # Reading pcode's own preferences is allowed; the provider's credential is not.
    read_text = Path.read_text
    forbidden = Mock(side_effect=AssertionError("must not read"))

    def guarded(self, *args, **kwargs):
        if self == auth:
            forbidden()
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)
    assert active_providers("anthropic:custom") == {"anthropic", "openai-codex"}
    forbidden.assert_not_called()


def test_proxy_does_not_limit_providers(monkeypatch):
    monkeypatch.setenv("PCODE_LLM_PROXY", "http://localhost:8080")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-key")
    assert active_providers("openai-codex:custom") == {"openai-codex", "anthropic"}


def test_anthropic_model_without_a_key_stays_unresolved():
    from pcode.agent import resolve_model

    # Unresolved, so the terminal opens and the first prompt explains the key.
    assert resolve_model("anthropic:claude-opus-5") == "anthropic:claude-opus-5"


@pytest.mark.parametrize("provider", sorted(PROVIDERS.keys() - {"claude", "antigravity"}))
def test_every_listed_provider_is_installed(provider):
    # A picker entry that raises ImportError on switch is worse than none.
    infer_provider_class(provider)


def test_claude_code_config_activates_claude(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert "claude" not in active_providers(None)
    (tmp_path / ".claude.json").touch()
    assert "claude" in active_providers(None)
    (tmp_path / ".claude.json").unlink()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "profile"))
    assert "claude" not in active_providers(None)
    (tmp_path / "profile").mkdir()
    assert "claude" in active_providers(None)
    # Picker entries are Anthropic's own model IDs.
    assert "claude:claude-opus-5-5" in model_catalog({"claude"})


def test_key_presence_enables_provider(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "synthetic")
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic")
    monkeypatch.setenv("AWS_PROFILE", "synthetic")
    assert active_providers(None) == {"groq", "google", "bedrock"}
    monkeypatch.setenv("GROQ_API_KEY", "  ")
    assert "groq" not in active_providers(None)


def test_endpoint_providers_need_both_variables(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "synthetic")
    assert active_providers(None) == set()
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.invalid")
    assert active_providers(None) == {"azure"}


def test_catalog_per_provider_and_typed_only_providers():
    models = model_catalog({"google", "xai", "openrouter", "claude"})
    assert any(name.startswith("google:gemini") for name in models)
    assert any(name.startswith("xai:grok") for name in models)
    assert any(name.startswith("claude:claude") for name in models)
    assert not any(name.startswith("openrouter:") for name in models)
    assert not any(name.startswith("anthropic:") for name in models)


def test_catalog_uses_installed_sdk_and_keeps_custom_current():
    models = model_catalog({"anthropic", "openai-codex"}, "anthropic:custom-id")
    assert "anthropic:custom-id" in models
    assert "anthropic:claude-opus-5-5" in models
    assert "openai-codex:gpt-5.6-luna" in models
    assert "openai-codex:gpt-6-astra" in models
    assert len(models) == len(set(models))
    assert all(name.startswith(("anthropic:", "openai-codex:")) for name in models)
    assert model_catalog(set()) == []
    assert all(name.startswith("anthropic:") for name in model_catalog({"anthropic"}))


def test_numeric_version_sort_newest_first_with_alias_before_snapshots():
    expected = [
        "anthropic:claude-opus-5",
        "anthropic:claude-opus-4-10",
        "anthropic:claude-opus-4-9",
        "anthropic:claude-opus-4-5",
        "anthropic:claude-opus-4-5-20251101",
        "anthropic:claude-opus-4-5-20251001",
        "anthropic:claude-sonnet-5",
        "openai-codex:gpt-5.10",
        "openai-codex:gpt-5.9",
        "openai-codex:gpt-5.6-luna",
    ]
    assert sorted(reversed(expected), key=model_sort_key) == expected


def test_latest_models_keeps_newest_of_each_family_in_order():
    names = [
        "anthropic:claude-opus-5-5",
        "anthropic:claude-opus-4-8",
        "anthropic:claude-opus-5-5-20261001",
        "anthropic:claude-3-opus-latest",
        "anthropic:claude-sonnet-4-6",
        "claude:claude-opus-4-8",
        "openai:gpt-6.2-sol",
        "openai:gpt-6.1-sol",
        "openai:gpt-6.1-terra",
        "openai:gpt-4o",
        "openai:gpt-4o-2024-08-06",
        "openai:gpt-5",
        "mistral:ministral-14b-latest",
        "mistral:ministral-8b-latest",
    ]
    assert latest_models(names) == [
        "anthropic:claude-opus-5-5",
        "anthropic:claude-sonnet-4-6",
        # Each provider keeps its own copy.
        "claude:claude-opus-4-8",
        "openai:gpt-6.2-sol",
        "openai:gpt-6.1-terra",
        # Digits glued to letters name a variant or size, not a release.
        "openai:gpt-4o",
        "openai:gpt-5",
        "mistral:ministral-14b-latest",
        "mistral:ministral-8b-latest",
    ]


def test_latest_models_reads_snapshot_dates_as_ties_not_versions():
    names = [
        "mistral:mistral-large-latest",
        "mistral:mistral-large-2411",
        "google-gla:gemini-2.5-flash-preview",
        "google-gla:gemini-2.5-flash-preview-09-2025",
        "google-gla:gemini-2.5-flash-preview-05-20",
    ]
    assert latest_models(names) == [
        "mistral:mistral-large-latest",
        "google-gla:gemini-2.5-flash-preview",
    ]


def test_latest_models_keeps_the_given_model():
    names = ["anthropic:claude-opus-5-5", "anthropic:claude-opus-4-8"]
    assert latest_models(names, keep="anthropic:claude-opus-4-8") == names


def test_current_model_does_not_override_version_order():
    current = "anthropic:claude-opus-4-8"
    models = model_catalog({"anthropic"}, current)
    assert models.index("anthropic:claude-opus-5-5") < models.index(current)


def test_google_cloud_uses_project_adc_when_gemini_api_key_is_also_set(monkeypatch):
    from pydantic_ai.models import infer_model
    from pydantic_ai.models.google import GoogleModel

    from pcode.agent import resolve_model

    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-gemini-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "synthetic-google-key")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-gcp-project")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)

    assert {"google", "google-cloud"} <= active_providers(None)

    resolved = resolve_model("google-cloud:gemini-2.5-pro")
    assert isinstance(resolved, GoogleModel)
    assert resolved.system == "google-cloud"
    assert resolved.model_name == "gemini-2.5-pro"
    assert resolved.client.vertexai is True
    assert resolved.client._api_client.api_key is None
    assert resolved.client._api_client.project == "my-gcp-project"
    assert resolved.client._api_client.location == "us-central1"

    # `google:` still uses the API key in the same environment.
    google_resolved = infer_model(resolve_model("google:gemini-2.5-pro"))
    assert isinstance(google_resolved, GoogleModel)
    assert google_resolved.system == "google"
    assert google_resolved.client.vertexai is False
    assert google_resolved.client._api_client.api_key == "synthetic-google-key"

    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
    resolved_global = resolve_model("google-cloud:gemini-2.5-pro")
    assert isinstance(resolved_global, GoogleModel)
    assert resolved_global.client._api_client.api_key is None
    assert resolved_global.client._api_client.location == "global"

    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "   ")
    resolved_blank_location = resolve_model("google-cloud:gemini-2.5-pro")
    assert isinstance(resolved_blank_location, GoogleModel)
    assert resolved_blank_location.client._api_client.location == "us-central1"


def test_google_cloud_without_project_stays_unresolved(monkeypatch):
    from pcode.agent import resolve_model

    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    assert resolve_model("google-cloud:gemini-2.5-pro") == "google-cloud:gemini-2.5-pro"
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "   ")
    assert resolve_model("google-cloud:gemini-2.5-pro") == "google-cloud:gemini-2.5-pro"


def test_recently_used_providers_lead_the_catalog_and_its_filtered_matches():
    from pcode.controller import SessionController
    from pcode.model_ui import model_items
    from pcode.pick_ui import matches
    from pcode.preferences import load_preferences, recent_providers

    providers = {"anthropic", "claude", "openai-codex"}
    assert model_catalog(providers)[0].startswith("anthropic:")

    # Saving a default model, as /model does, records its provider.
    SessionController.persist_defaults(Mock(), model="anthropic:claude-opus-5-5")
    SessionController.persist_defaults(Mock(), model="claude:claude-opus-5-5")
    assert recent_providers() == ["claude", "anthropic"]
    assert load_preferences()["model"] == "claude:claude-opus-5-5"

    models = model_catalog(providers)
    order = list(dict.fromkeys(name.partition(":")[0] for name in models))
    assert order == ["claude", "anthropic", "openai-codex"]
    # Within a provider, versions keep their newest-first order.
    claude = [name for name in models if name.startswith("claude:")]
    assert claude == sorted(claude, key=model_sort_key)
    # A filter matching the same model under both providers offers claude's first.
    shown = [item.id for item in model_items(models) if matches(item, ["opus"])]
    assert shown[0].startswith("claude:")


def test_a_bare_name_resolves_under_the_sessions_provider():
    from pcode.models import qualify_model

    assert qualify_model("claude-fable-5-2", "claude:claude-opus-5-5") == "claude:claude-fable-5-2"
    # The session's own model qualifies even when the catalog does not list it.
    assert qualify_model("claude-next", "claude:claude-next") == "claude:claude-next"
    # A provider without a catalog takes any id, `:` and all.
    assert qualify_model("llama3:8b", "ollama:qwen3") == "ollama:llama3:8b"
    # A model object names its provider by its `system`.
    session = Mock(system="claude", model_id="claude:claude-opus-5-5")
    assert qualify_model("claude-fable-5-2", session) == "claude:claude-fable-5-2"


def test_an_explicit_provider_is_never_rewritten():
    from pcode.models import qualify_model

    for name in ("anthropic:claude-fable-5-2", "openai:gpt-5", "claude:claude-opus-5-5"):
        assert qualify_model(name, "claude:claude-opus-5-5") == name


def test_a_bare_name_the_session_provider_lacks_is_left_for_inference():
    from pcode.models import qualify_model

    assert qualify_model("gpt-5", "claude:claude-opus-5-5") == "gpt-5"
    # No session, or one on a provider pcode does not know: nothing to qualify with.
    assert qualify_model("claude-fable-5-2") == "claude-fable-5-2"
    assert qualify_model("claude-fable-5-2", Mock(system="function")) == "claude-fable-5-2"
