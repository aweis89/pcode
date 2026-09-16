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
        assert "prompt_cache_breakpoint" not in json.dumps(payloads[0])

    asyncio.run(run())
