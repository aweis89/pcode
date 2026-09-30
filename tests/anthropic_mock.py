"""Answer a mocked Anthropic Messages request in the shape it asked for.

Pydantic AI streams an Anthropic request whose default `max_tokens` (the
model's maximum output) is too large for a plain one, even from `agent.run`.
A mock that always answers JSON then yields "Streamed response ended without
content or tool calls", so answer a streamed request as server-sent events.
"""

import json

import httpx2


def _sse(message: dict) -> bytes:
    events = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {**message, "content": [], "stop_reason": None},
            },
        )
    ]
    for index, block in enumerate(message["content"]):
        if block["type"] == "text":
            start, delta = {**block, "text": ""}, {"type": "text_delta", "text": block["text"]}
        elif block["type"] == "thinking":
            start = {**block, "thinking": ""}
            delta = {"type": "thinking_delta", "thinking": block["thinking"]}
        elif block["type"] in ("tool_use", "server_tool_use"):
            start = {**block, "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        else:
            start, delta = block, None
        events.append(
            (
                "content_block_start",
                {"type": "content_block_start", "index": index, "content_block": start},
            )
        )
        if delta is not None:
            events.append(
                (
                    "content_block_delta",
                    {"type": "content_block_delta", "index": index, "delta": delta},
                )
            )
        events.append(("content_block_stop", {"type": "content_block_stop", "index": index}))
    events.append(
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": message.get("stop_reason"), "stop_sequence": None},
                "usage": message.get("usage", {"output_tokens": 0}),
            },
        )
    )
    events.append(("message_stop", {"type": "message_stop"}))
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


def anthropic_response(request, message: dict, **kwargs) -> httpx2.Response:
    """`message` as a JSON body, or as an event stream when `request` asked to stream."""
    if json.loads(request.content).get("stream"):
        return httpx2.Response(
            200,
            content=_sse(message),
            headers={"content-type": "text/event-stream", **kwargs.pop("headers", {})},
            **kwargs,
        )
    return httpx2.Response(200, json=message, **kwargs)


# Instructions Harness reads from the workspace at run start. Pydantic AI 2.52
# places the instructions breakpoint after the static ones, so these follow it.
WORKSPACE_DERIVED = (
    "File tools accept",
    "<context-file",
    "<assistant-configuration",
    "No assistant configuration",
)


def assert_instructions_breakpoint(system):
    """The last static block carries the breakpoint; only workspace-derived ones follow."""
    marked = [index for index, block in enumerate(system) if block.get("cache_control")]
    assert marked, "no instructions breakpoint"
    after = system[marked[-1] + 1 :]
    assert all(block["text"].startswith(WORKSPACE_DERIVED) for block in after), [
        block["text"][:40] for block in after
    ]
