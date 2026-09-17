import asyncio
import json

import httpx2
import pytest
from pydantic_ai import CachePoint
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.openai_codex import OpenAICodexModel
from pydantic_ai.providers.openai_codex import OpenAICodexCredentials, OpenAICodexProvider

from pcode.agent import create_agent


def test_native_codex_wire_payload_omits_unsupported_cache_marker(monkeypatch, tmp_path):
    payloads = []

    def handle(request):
        payloads.append(json.loads(request.content))
        return httpx2.Response(400, json={"error": {"message": "end of mock request"}})

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
            provider = OpenAICodexProvider(
                credentials=OpenAICodexCredentials(
                    access_token="test-access",
                    refresh_token="test-refresh",
                    account_id="test-account",
                ),
                http_client=client,
            )

            def native_model(name, *, profile):
                return OpenAICodexModel(name, provider=provider, profile=profile)

            monkeypatch.setattr("pcode.agent.OpenAICodexModel", native_model)
            agent = create_agent("openai-codex:gpt-5.6-sol", tmp_path)
            with pytest.raises(ModelHTTPError):
                await agent.run(["A durable user message", CachePoint()])
        assert len(payloads) == 1
        assert payloads[0]["model"] == "gpt-5.6-sol"
        assert payloads[0]["store"] is False
        assert payloads[0]["stream"] is True
        assert payloads[0]["reasoning"]["summary"] == "auto"
        assert "prompt_cache_breakpoint" not in json.dumps(payloads[0])

    asyncio.run(run())


def test_codex_summary_request_is_independent_of_display_and_effort(monkeypatch, tmp_path):
    from pcode.preferences import apply_effort, save_preferences

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for visibility in ("off", "on"):
        save_preferences(show_thinking=visibility)
        agent = create_agent("openai-codex:gpt-5.6-sol", tmp_path)
        assert agent.model_settings == {"openai_reasoning_summary": "auto"}
        apply_effort(agent, "openai-codex:gpt-5.6-sol", "medium")
        assert agent.model_settings == {
            "openai_reasoning_summary": "auto",
            "openai_reasoning_effort": "medium",
        }
        apply_effort(agent, "openai-codex:gpt-5.6-sol", "default")
        assert agent.model_settings == {"openai_reasoning_summary": "auto"}


def test_non_codex_model_does_not_receive_codex_summary_setting(tmp_path):
    assert create_agent("test", tmp_path).model_settings is None
