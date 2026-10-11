"""The CLI's own `WebSearch`/`WebFetch`, presented as Anthropic server tools.

The CLI runs both itself, between two API messages of one turn. pcode shows
them the way it shows Anthropic's native web tools: a `server_tool_use` block
and a result block, built here in the shapes Pydantic AI parses.

The blocks are pcode's record only; the model reads the CLI's own transcript.
They are tagged with the `claude` provider, so no other model is ever sent
them, and they are used just for display and for a fresh replay
(`messages.replay`).

What the CLI returns has no slot in Anthropic's shapes, so it rides in extra
fields, which Pydantic AI keeps: a search's summary on its first hit
(`summary`), and a failure's full text beside its code (`message`). Pydantic
AI also serializes these blocks against the SDK types, so `error_code` must
stay one of Anthropic's codes or every result logs a serializer warning.
"""

import json
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# CLI tool name -> Anthropic server tool name (also the native tool's `kind`).
CLI_WEB_TOOLS = {"WebSearch": "web_search", "WebFetch": "web_fetch"}

# The CLI's search result opens with its query, then this JSON list of hits,
# then a summary its own sub-request wrote.
_LINKS = re.compile(r"^Links: (\[.*\])$", re.MULTILINE)
_QUERY = re.compile(r"\AWeb search results for query: .*$", re.MULTILINE)
# The CLI's closing instruction to its model, not part of the summary.
_REMINDER = re.compile(r"\n*REMINDER: You MUST include the sources.*\Z", re.DOTALL)


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


def _hit(title: str, url: str) -> dict:
    return {
        "type": "web_search_result",
        "title": title,
        "url": url,
        # Pydantic AI's result model requires both; the CLI has neither.
        "encrypted_content": "",
        "page_age": None,
    }


def _hits(text: str) -> list[dict]:
    """The hits in the CLI's search text, the first carrying its summary.

    Text with no parseable `Links:` line means the CLI changed its format (an
    empty search still prints `Links: []`), so that is logged rather than
    shown as a quiet "0 results"; the text is kept on a URL-less hit.
    """
    match = _LINKS.search(text)
    try:
        links = json.loads(match.group(1)) if match else None
    except ValueError:
        links = None
    if links is None:
        logger.warning("Claude Code's WebSearch result has no parseable Links line: %.200r", text)
        summary = _QUERY.sub("", text)
    else:
        summary = text[match.end() :] if match else ""
    summary = _REMINDER.sub("", summary).strip()
    hits = [
        _hit(str(link.get("title") or ""), str(link["url"]))
        for link in links or []
        if isinstance(link, dict) and link.get("url")
    ]
    if summary:
        hits = hits or [_hit("", "")]
        hits[0]["summary"] = summary
    return hits


def result_block(name: str, tool_use_id: str, args: dict, content: object, is_error: bool) -> dict:
    """The CLI's result for a web tool call as the server tool's result block."""
    server = CLI_WEB_TOOLS[name]
    text = _text(content)
    if is_error:
        body: object = {
            "type": f"{server}_tool_result_error",
            "error_code": "unavailable",
            "message": text.strip(),
        }
    elif server == "web_search":
        body = _hits(text)
    else:
        body = {
            "type": "web_fetch_result",
            "url": str(args.get("url") or ""),
            # When the CLI handed the answer over, which is when it fetched.
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "content": {
                "type": "document",
                "source": {"type": "text", "media_type": "text/plain", "data": text},
                "title": None,
            },
        }
    return {"type": f"{server}_tool_result", "tool_use_id": tool_use_id, "content": body}
