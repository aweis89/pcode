"""History bookkeeping: normalizing, hashing and replaying Anthropic messages."""

import base64
import hashlib
import io
import json
from typing import Any

REPLAY_INTRO = (
    "This conversation began outside the current session, so its earlier messages are "
    "replayed below as a transcript. Treat them as having happened here and continue "
    "from the final message.\n\n"
)


def _clean(value: Any) -> Any:
    """Drop cache markers and make binary sources plain base64 JSON.

    The CLI places its own cache markers, and they never change content.
    Pydantic AI maps image and PDF bytes to `io.BytesIO`, which the Anthropic
    SDK encodes on send; the CLI takes JSON, and hashing needs stable values.
    """
    if isinstance(value, io.BytesIO):
        return base64.b64encode(value.getvalue()).decode("ascii")
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _blocks(message: dict) -> list[dict]:
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    return list(content or [])


def normalize(messages: list[dict]) -> list[dict]:
    """Anthropic messages without cache markers, consecutive same-role turns merged.

    Pydantic AI leaves an appended reminder as its own user message; the API
    merges consecutive turns anyway, and one user turn is one CLI input.
    """
    merged: list[dict] = []
    for message in _clean(messages):
        if merged and merged[-1]["role"] == message["role"]:
            merged[-1] = {
                "role": message["role"],
                "content": _blocks(merged[-1]) + _blocks(message),
            }
        else:
            merged.append({"role": message["role"], "content": _blocks(message)})
    return merged


def _digest(previous: str, message: dict) -> str:
    text = json.dumps(message, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{previous}\n{text}".encode()).hexdigest()


def lineage(messages: list[dict]) -> list[str]:
    """One hash per message, each covering the whole history up to it."""
    chain, previous = [], "pcode-claude-1"
    for message in messages:
        previous = _digest(previous, message)
        chain.append(previous)
    return chain


def _tool_use_ids(message: dict) -> tuple[str, ...]:
    return tuple(b["id"] for b in _blocks(message) if b.get("type") == "tool_use")


def _result_ids(message: dict) -> set[str]:
    return {b["tool_use_id"] for b in _blocks(message) if b.get("type") == "tool_result"}


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    return "\n".join(_render_block(item) for item in content or [])


def _render_block(block: dict) -> str:
    kind = block.get("type")
    if kind == "text":
        return block.get("text", "")
    if kind == "tool_use":
        arguments = json.dumps(block.get("input"), ensure_ascii=False, default=str)
        return f"[Called tool {block.get('name')} (id {block.get('id')}) with {arguments}]"
    if kind == "tool_result":
        status = " (error)" if block.get("is_error") else ""
        return f"[Result of tool call {block.get('tool_use_id')}{status}]\n{_result_text(block)}"
    if kind in ("thinking", "redacted_thinking"):
        return ""
    return f"[{kind or 'content'} omitted]"


def replay(messages: list[dict], open_tool_ids: tuple[str, ...] = ()) -> list[dict]:
    """One user turn carrying `messages`, for a CLI transcript that lacks them.

    Results for the tool calls the transcript ends on (`open_tool_ids`) stay
    structured, since the API requires them; everything else becomes text.
    Images and documents from the final message are still attached.
    """
    first = _blocks(messages[0]) if messages else []
    head = [
        b for b in first if b.get("type") == "tool_result" and b["tool_use_id"] in open_tool_ids
    ]
    answered = {b["tool_use_id"] for b in head}
    head += [
        {
            "type": "tool_result",
            "tool_use_id": i,
            "content": "[No result recorded]",
            "is_error": True,
        }
        for i in open_tool_ids
        if i not in answered
    ]
    rest = [
        {"role": messages[0]["role"], "content": [b for b in first if b not in head]},
        *messages[1:],
    ]
    turns = []
    for message in rest:
        text = "\n".join(filter(None, (_render_block(b) for b in _blocks(message))))
        if text:
            turns.append(f"[{message['role'].title()}]\n{text}")
    attached = [b for b in _blocks(messages[-1]) if b.get("type") in ("image", "document")]
    body = "\n\n".join(turns)
    text = f"{REPLAY_INTRO}<conversation>\n{body}\n</conversation>" if turns else ""
    return head + ([{"type": "text", "text": text}] if text else []) + attached


def _mcp_content(block: dict) -> list[dict]:
    """An Anthropic tool_result's content as MCP content blocks."""
    content = block.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    items = []
    for item in content or []:
        source = item.get("source") or {}
        if item.get("type") == "text":
            items.append({"type": "text", "text": item.get("text", "")})
        elif item.get("type") == "image" and source.get("type") == "base64":
            items.append(
                {"type": "image", "data": source["data"], "mimeType": source["media_type"]}
            )
        else:
            items.append({"type": "text", "text": _render_block(item)})
    return items
