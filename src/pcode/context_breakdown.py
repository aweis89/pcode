"""Attribute fixed prompt overhead to the thing that contributed it.

`context_usage` reports one number for the whole request, which says nothing
about *why* it is that large. Pydantic AI already tags every instruction part
with the capability that authored it, so the breakdown here is a grouping of
`ModelRequestParameters`, not a second estimate of the prompt.

Repository instructions arrive as one part holding several `<context-file>`
blocks; they are split for display only. Nothing in this module changes what is
sent, so a wrong label can never cost a cache hit.
"""

import json
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from pcode.context_usage import compact_tokens

# `compaction.schema_tokens` uses the same divisor. Provider tokenizers disagree
# with each other anyway; a consistent estimate keeps these rows comparable with
# the compaction threshold that acts on them.
CHARS_PER_TOKEN = 4

# An instruction part is attributed to its source only when that source has an
# `id`, so pcode names the ones it constructs (see `agent.create_coder`) and reads
# Harness's own ids for the rest. Anything unlisted falls back to the bare id.
REPO_CONTEXT = "repo_context"
SOURCE_LABELS = {
    REPO_CONTEXT: "Repository context",
    "code_mode": "Code mode",
    "file_tools": "File tools",
    "mcp_servers": "MCP servers",
    "planning": "Planning tool",
    "sub_agents": "Sub-agents",
    "tool_output_limits": "Tool output limits",
    "ext.web_research": "Web research",
}
AGENT_LABEL = "Terminal instructions"
# Coder's base prompt is a concrete `Capability`, which attributes its
# instructions at construction and so cannot be named after the fact.
ANONYMOUS_LABEL = "Harness base prompts"

CONTEXT_FILE = re.compile(r'<context-file path="(?P<path>[^"]*)">\n.*?\n</context-file>', re.DOTALL)
ASSISTANT_CONFIG = re.compile(
    r"<assistant-configuration>\n.*?\n</assistant-configuration>", re.DOTALL
)


def estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


@dataclass(frozen=True)
class Row:
    """One attributed slice of the fixed prompt overhead."""

    label: str
    tokens: int
    detail: str = ""


def source_id(part) -> str | None:
    """Name the capability that authored `part`; None for the agent's own or anonymous."""
    return getattr(getattr(getattr(part, "id", None), "source", None), "id", None)


def _short(path: str) -> str:
    """Shorten a home-relative instruction path the way a shell prompt would."""
    candidate = Path(path)
    return (
        f"~/{candidate.relative_to(Path.home())}"
        if candidate.is_absolute() and candidate.is_relative_to(Path.home())
        else path
    )


def _asset_detail(block: str) -> str:
    """Count the discovered assets the block lists, without re-scanning the disk."""
    try:
        inventory = next(
            (json.loads(line) for line in block.splitlines() if line.startswith("{")), None
        )
    except ValueError:
        # An unreadable inventory is still a measurable number of tokens, and
        # /status must not be the thing that fails on it.
        inventory = None
    if inventory is None:
        return "paths only"
    counts = [
        (sum(len(root.get(key, ())) for root in inventory.get("roots", ())), noun)
        for key, noun in (("skills", "skill"), ("agents", "agent"))
    ]
    listed = ", ".join(f"{count} {noun}" for count, noun in counts if count)
    return f"paths only · {listed}" if listed else "paths only · nothing discovered"


def repo_rows(text: str) -> Iterator[Row]:
    """Split repository context into the instruction files and the asset inventory."""
    remainder = text
    for match in CONTEXT_FILE.finditer(text):
        remainder = remainder.replace(match.group(), "", 1)
        yield Row(_short(match["path"]), estimate_tokens(match.group()), "instructions")
    if config := ASSISTANT_CONFIG.search(text):
        remainder = remainder.replace(config.group(), "", 1)
        yield Row(
            "Assistant config", estimate_tokens(config.group()), _asset_detail(config.group())
        )
    if leftover := remainder.strip():
        yield Row(SOURCE_LABELS[REPO_CONTEXT], estimate_tokens(leftover))


def instruction_rows(parts: Sequence) -> list[Row]:
    """One row per instruction source, largest first, with repository context expanded."""
    rows: list[Row] = []
    anonymous = 0
    for part in parts or ():
        identity = source_id(part)
        if identity == REPO_CONTEXT:
            rows.extend(repo_rows(part.content))
        elif identity:
            rows.append(Row(SOURCE_LABELS.get(identity, identity), estimate_tokens(part.content)))
        elif getattr(part, "id", None) is None:
            anonymous += estimate_tokens(part.content)
        else:
            rows.append(Row(AGENT_LABEL, estimate_tokens(part.content)))
    if anonymous:
        rows.append(Row(ANONYMOUS_LABEL, anonymous))
    return sorted(rows, key=lambda row: -row.tokens)


def tool_rows(parameters) -> list[Row]:
    """Size each tool the way `compaction.schema_tokens` sizes them in aggregate."""
    return sorted(
        (
            Row(
                tool.name,
                estimate_tokens(json.dumps(tool.parameters_json_schema) + (tool.description or "")),
            )
            for tool in [*parameters.function_tools, *parameters.output_tools]
        ),
        key=lambda row: -row.tokens,
    )


def overhead_rows(parameters, *, window: int | None = None) -> list[tuple[str, str]]:
    """Label/value rows describing the fixed cost of the last request's prompt.

    Fixed means what the provider is re-sent whatever the conversation did:
    instructions and tool schemas, never message history.
    """
    instructions = instruction_rows(parameters.instruction_parts or [])
    tools = tool_rows(parameters)
    prompt = sum(row.tokens for row in instructions)
    schemas = sum(row.tokens for row in tools)
    total = prompt + schemas
    share = f" · {total * 100 // window}% of {compact_tokens(window)}" if window else ""
    rows = [
        ("Prompt overhead", f"~{compact_tokens(total)} tokens{share} · estimated"),
        ("  Instructions", f"~{compact_tokens(prompt)}"),
    ]
    for row in instructions:
        rows.append(
            (
                f"    {row.label}",
                f"~{compact_tokens(row.tokens)}" + (f" · {row.detail}" if row.detail else ""),
            )
        )
    rows.append(("  Tool schemas", f"~{compact_tokens(schemas)} · {len(tools)} tools"))
    if tools:
        rows.append(
            (
                "    Largest",
                " · ".join(f"{row.label} {compact_tokens(row.tokens)}" for row in tools[:5]),
            )
        )
    return rows
