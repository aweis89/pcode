import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai_harness.exa import ExaSearch

from pcode.agent import create_coder


@pytest.mark.parametrize("key", [None, "", "   "])
def test_coder_without_exa_key_keeps_coding_tools(tmp_path, monkeypatch, key):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    if key is not None:
        monkeypatch.setenv("EXA_API_KEY", key)
    coder = create_coder(tmp_path)
    assert not any(isinstance(c, ExaSearch) for c in coder.capabilities)

    async def model(messages, info):
        names = {tool.name for tool in info.function_tools}
        assert {"read_file", "edit_file", "shell"} <= names
        assert not {"web_search", "get_page", "deep_search"} & names
        yield "Coding without search."

    Agent(FunctionModel(stream_function=model), capabilities=[coder]).run_sync("Hello")


def test_coder_search_uses_default_client_and_returns_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "test-exa-key")
    coder = create_coder(tmp_path)
    search = next(c for c in coder.capabilities if isinstance(c, ExaSearch))
    assert search.client is None  # Harness resolves EXA_API_KEY, not the model.
    assert not search.include_deep_search
    result = SimpleNamespace(
        url="https://example.com/docs",
        title="Example docs",
        published_date=None,
        author=None,
        highlights=["Search excerpt"],
        text="Full page content",
    )
    client = SimpleNamespace(
        search=AsyncMock(return_value=SimpleNamespace(results=[result], output=None)),
        get_contents=AsyncMock(return_value=SimpleNamespace(results=[result])),
    )
    requests = 0

    async def model(messages, info):
        nonlocal requests
        requests += 1
        names = {tool.name for tool in info.function_tools}
        assert {"web_search", "get_page", "read_file", "shell"} <= names
        assert "deep_search" not in names
        assert "test-exa-key" not in str(info)
        if requests == 1:
            yield {0: DeltaToolCall(name="web_search", json_args='{"query":"example docs"}')}
            return
        parts = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        last = parts[-1]
        assert "https://example.com/docs" in str(last.content)
        assert last.metadata["sources"] == [
            {"url": "https://example.com/docs", "title": "Example docs"}
        ]
        if requests == 2:
            assert "Search excerpt" in str(last.content)
            yield {
                0: DeltaToolCall(name="get_page", json_args='{"url":"https://example.com/docs"}')
            }
        else:
            assert "Full page content" in str(last.content)
            yield "Found the docs."

    async def run():
        with patch("pydantic_ai_harness.exa._toolset.AsyncExa", return_value=client) as ctor:
            agent = Agent(FunctionModel(stream_function=model), capabilities=[coder])
            response = await agent.run("Find example docs")
            assert response.output == "Found the docs."
            assert ctor.call_count >= 1
            assert all(args == () and kwargs == {} for args, kwargs in ctor.call_args_list)
        client.search.assert_awaited_once_with(
            "example docs",
            contents={"highlights": True},
            num_results=5,
            output_schema=None,
            include_domains=None,
            exclude_domains=None,
        )
        client.get_contents.assert_awaited_once_with(
            "https://example.com/docs", text={"max_characters": 10_000}
        )

    asyncio.run(run())
