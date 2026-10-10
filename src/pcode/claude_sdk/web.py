"""The CLI's own `WebSearch`/`WebFetch`, presented as Anthropic server tools.

The CLI runs both itself, between two API messages of one turn. pcode shows
them the way it shows Anthropic's native web tools: a `server_tool_use` block
and a result block, built here in the shapes Pydantic AI parses.

The blocks are pcode's record only; the model reads the CLI's own transcript.
They are tagged with the `claude` provider, so no other model is ever sent
them, and they are used just for display and for a fresh replay
(`messages.replay`).
"""

import json
import re

# CLI tool name -> Anthropic server tool name (also the native tool's `kind`).
CLI_WEB_TOOLS = {"WebSearch": "web_search", "WebFetch": "web_fetch"}

# The CLI's search result opens with its query, then this JSON list of hits,
# then a summary its own sub-request wrote.
_LINKS = re.compile(r"^Links: (\[.*\])$", re.MULTILINE)


def cli_tool_names(native_tools) -> tuple[str, ...]:
    """The CLI tools that stand in for the native tools a request asks for."""
    wanted = {tool.kind for tool in native_tools}
    return tuple(sorted(cli for cli, server in CLI_WEB_TOOLS.items() if server in wanted))


def server_tool_use(block: dict) -> dict:
    """A CLI web `tool_use` block as the `server_tool_use` block it stands for."""
    return {**block, "type": "server_tool_use", "name": CLI_WEB_TOOLS[block["name"]]}


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
    return ""


def _hits(text: str) -> list[dict]:
    match = _LINKS.search(text)
    try:
        links = json.loads(match.group(1)) if match else []
    except ValueError:
        links = []
    return [
        {
            "type": "web_search_result",
            "title": str(link.get("title") or ""),
            "url": str(link["url"]),
            # Pydantic AI's result model requires both; the CLI has neither.
            "encrypted_content": "",
            "page_age": None,
        }
        for link in links
        if isinstance(link, dict) and link.get("url")
    ]


def result_block(name: str, tool_use_id: str, args: dict, content: object, is_error: bool) -> dict:
    """The CLI's result for a web tool call as the server tool's result block."""
    server = CLI_WEB_TOOLS[name]
    text = _text(content)
    if is_error:
        # Anthropic's error block holds a code only; the CLI's message is more
        # use to whoever reads the failure, so it travels in that field.
        first = text.strip().splitlines()[0] if text.strip() else "unavailable"
        body: object = {"type": f"{server}_tool_result_error", "error_code": first[:200]}
    elif server == "web_search":
        body = _hits(text)
    else:
        body = {
            "type": "web_fetch_result",
            "url": str(args.get("url") or ""),
            "retrieved_at": None,
            "content": {
                "type": "document",
                "source": {"type": "text", "media_type": "text/plain", "data": text},
                "title": None,
            },
        }
    return {"type": f"{server}_tool_result", "tool_use_id": tool_use_id, "content": body}
