"""A history signed for one account must not strand a session on the next.

Anthropic encrypts every server-side search result to the organization that
issued it, so resuming under a different login rejects the whole history with
a 400 that no retry can clear. The runtime drops those results and sends again.
"""

import asyncio
import json

import httpx2
import pytest
from anthropic import AsyncAnthropic
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    NativeToolCallPart,
    NativeToolReturnPart,
    TextPart,
    ThinkingPart,
    UserPromptPart,
)
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

from pcode.live import AgentRuntime
from pcode.native_results import drop_unreadable_results, unreadable_native_results

URL = "https://learn.microsoft.com/azure/deployments"
REJECTION = {
    "type": "error",
    "error": {
        "type": "invalid_request_error",
        "message": "messages.3.content.0: Invalid `encrypted_content` in `search_result` block",
    },
}


def searched(query: str = "azure deployment 404") -> ModelResponse:
    """One response shaped like a completed native search, blobs and all."""
    return ModelResponse(
        parts=[
            NativeToolCallPart(
                tool_name="web_search",
                tool_call_id="srvtoolu_1",
                args={"query": query},
                provider_name="anthropic",
            ),
            NativeToolReturnPart(
                tool_name="web_search",
                tool_call_id="srvtoolu_1",
                provider_name="anthropic",
                content=[
                    {
                        "type": "web_search_result",
                        "url": URL,
                        "title": "Deployment operations",
                        "page_age": None,
                        "encrypted_content": "Etc0signed-for-another-org",
                    }
                ],
            ),
            ThinkingPart(content="considering", signature="sig", provider_name="anthropic"),
            TextPart(content="The deployment was recreated."),
        ]
    )


def history() -> list:
    return [ModelRequest(parts=[UserPromptPart(content="why did it drift?")]), searched()]


def test_only_an_encrypted_content_rejection_counts_as_unreadable():
    assert unreadable_native_results(ModelHTTPError(400, "claude", body=REJECTION))
    # A request pcode built wrong is a bug to surface, not a history to edit.
    assert not unreadable_native_results(ModelHTTPError(400, "claude", body={"error": "no tools"}))
    # The same text from a server error says nothing about the history.
    assert not unreadable_native_results(ModelHTTPError(500, "claude", body=REJECTION))
    assert not unreadable_native_results(ModelAPIError("claude", "Connection error."))


def test_dropping_keeps_the_trace_and_repairs_every_holder():
    messages = history()
    # Two references to the one response, as the turn, its checkpoint and the
    # conversation tree all hold: repairing the list would fix only this name.
    also_held = list(messages)

    assert drop_unreadable_results(messages) == 1

    parts = also_held[1].parts
    assert [type(part).__name__ for part in parts] == ["TextPart", "ThinkingPart", "TextPart"]
    note = parts[0].content
    assert "azure deployment 404" in note
    assert URL in note
    assert "encrypted" in note
    # The model's own reading of the search is untouched, and so is a thinking
    # signature: those replay across accounts.
    assert parts[2].content == "The deployment was recreated."
    assert parts[1].signature == "sig"


def test_a_history_without_encrypted_results_is_left_alone():
    messages = [ModelRequest(parts=[UserPromptPart(content="hi")]), ModelResponse(parts=[])]
    plain = ModelResponse(
        parts=[
            NativeToolCallPart(
                tool_name="web_fetch", tool_call_id="s1", args={}, provider_name="anthropic"
            ),
            NativeToolReturnPart(
                tool_name="web_fetch",
                tool_call_id="s1",
                provider_name="anthropic",
                content={"type": "web_fetch_result", "url": URL},
            ),
        ]
    )
    messages.append(plain)
    assert drop_unreadable_results(messages) == 0
    assert len(plain.parts) == 2


def sse(events: list[tuple[str, dict]]) -> str:
    return "".join(
        f"event: {kind}\ndata: {json.dumps({'type': kind, **payload})}\n\n"
        for kind, payload in events
    )


def reply(text: str) -> str:
    return sse(
        [
            (
                "message_start",
                {
                    "message": {
                        "id": "msg",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude-opus-5",
                        "content": [],
                        "usage": {"input_tokens": 1, "output_tokens": 0},
                    }
                },
            ),
            ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
            (
                "content_block_delta",
                {"index": 0, "delta": {"type": "text_delta", "text": text}},
            ),
            ("content_block_stop", {"index": 0}),
            (
                "message_delta",
                {"delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {}},
            ),
            ("message_stop", {}),
        ]
    )


def runtime_with(handler) -> AgentRuntime:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    model = AnthropicModel(
        "claude-opus-5",
        provider=AnthropicProvider(
            anthropic_client=AsyncAnthropic(api_key="t", http_client=client, max_retries=0)
        ),
    )
    runtime = AgentRuntime(Agent(model, retries=0))
    # No transport-retry budget: the repair must stand on its own.
    runtime.retry_attempts = 0
    runtime.history = history()
    return runtime


def drain(runtime: AgentRuntime, prompt: str = "and now?"):
    async def run() -> None:
        async for _ in runtime.stream(prompt):
            pass

    return run


def test_a_rejected_history_is_repaired_and_the_turn_sent_again():
    bodies = []
    notices = []

    def handle(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx2.Response(400, json=REJECTION)
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=reply("Recovered.")
        )

    runtime = runtime_with(handle)
    runtime.retry_notice = notices.append
    try:
        asyncio.run(drain(runtime)())
    finally:
        runtime.close()

    assert len(bodies) == 2
    assert "encrypted_content" in json.dumps(bodies[0])
    assert "encrypted_content" not in json.dumps(bodies[1])
    # The evidence the model can still act on survives the repair.
    assert URL in json.dumps(bodies[1])
    assert notices and "web search" in notices[0]


def test_a_history_that_stays_rejected_fails_instead_of_looping():
    bodies = []

    def handle(request):
        bodies.append(request)
        return httpx2.Response(400, json=REJECTION)

    runtime = runtime_with(handle)
    try:
        with pytest.raises(ModelHTTPError):
            asyncio.run(drain(runtime)())
    finally:
        runtime.close()

    # One repair attempt, then the failure is the user's to see.
    assert len(bodies) == 2
