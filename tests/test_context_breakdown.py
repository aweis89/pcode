"""Prompt overhead is attributed from what was sent, and attribution never changes it."""

import asyncio
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import InstructionPart
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import ToolDefinition

from pcode.agent import create_coder
from pcode.context_breakdown import (
    AGENT_LABEL,
    ANONYMOUS_LABEL,
    REPO_CONTEXT,
    instruction_rows,
    overhead_rows,
    repo_rows,
    source_id,
    tool_rows,
)

REPO_CONTEXT_PROMPT = """<context-file path="{home}/AGENTS.md">
Global rules.
</context-file>

<context-file path="AGENTS.md">
Repository rules.
</context-file>

<assistant-configuration>
Automatically discovered assistant configuration paths.
{{"roots":[{{"root":".agents","exists":true,"skills":[".agents/skills/a/SKILL.md"],"agents":[]}}]}}
</assistant-configuration>"""


@dataclass
class Probe(AbstractCapability):
    """Stop the run once the resolved request parameters have been captured."""

    captured: dict = None

    async def before_model_request(self, ctx, request_context):
        self.captured["parameters"] = request_context.model_request_parameters
        self.captured["instructions"] = request_context.messages[-1].instructions
        raise Stopped()


class Stopped(Exception):
    pass


def isolated_workspace():
    """A repo inside the patched HOME, so walk-up cannot reach the developer's AGENTS.md."""
    workspace = Path.home() / "repo"
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def request_parameters(workspace):
    captured = {}
    agent = Agent(
        TestModel(),
        instructions="Terminal rendering notes.",
        capabilities=[create_coder(workspace), Probe(captured=captured)],
    )
    try:
        asyncio.run(agent.run("hi"))
    except* Stopped:
        pass
    return captured


def part(content, identity=None):
    """Build a part the way the framework attributes one to a capability."""
    source = None if identity is None else CapabilitySource(identity)
    return InstructionPart(content=content, id=None if source is None else Identity(source))


@dataclass(frozen=True)
class CapabilitySource:
    id: str


@dataclass(frozen=True)
class Identity:
    source: object


def test_repo_context_splits_into_files_and_asset_inventory():
    rows = list(repo_rows(REPO_CONTEXT_PROMPT.format(home=Path.home())))
    assert [row.label for row in rows] == ["~/AGENTS.md", "AGENTS.md", "Assistant config"]
    assert rows[0].detail == "instructions"
    # Skills cost a path, not a body: the count belongs next to those few tokens.
    assert rows[-1].detail == "paths only · 1 skill"
    assert all(row.tokens > 0 for row in rows)


def test_inventory_without_assets_says_so_rather_than_counting_substrings():
    block = (
        "<assistant-configuration>\nPaths only.\n"
        '{"roots":[{"root":".agents","exists":true,"skills":[],"agents":[]}]}\n'
        "</assistant-configuration>"
    )
    assert list(repo_rows(block))[0].detail == "paths only · nothing discovered"


def test_rows_are_ordered_by_cost_and_group_anonymous_sources():
    rows = instruction_rows(
        [
            part("planning " * 10, "planning"),
            part("anonymous " * 40),
            part("more anonymous " * 40),
            part("web " * 5, "ext.web_research"),
        ]
    )
    assert [row.label for row in rows] == [ANONYMOUS_LABEL, "Planning tool", "Web research"]
    assert rows[0].tokens > rows[1].tokens > rows[2].tokens


def test_named_capabilities_leave_the_rendered_prompt_byte_identical():
    """Ids are display metadata; naming a source must not move a cache boundary."""
    captured = request_parameters(isolated_workspace())
    parts = captured["parameters"].instruction_parts
    assert "\n\n".join(item.content for item in parts) == captured["instructions"]
    # Every label in SOURCE_LABELS has to be reachable from a real composition.
    assert {source_id(item) for item in parts} >= {
        REPO_CONTEXT,
        "file_tools",
        "planning",
        "sub_agents",
    }
    # Coder's base prompt stays anonymous, so it must not be silently dropped.
    labels = {row.label for row in instruction_rows(parts)}
    assert ANONYMOUS_LABEL in labels and AGENT_LABEL in labels


def test_overhead_reports_instructions_and_schemas_from_a_real_request():
    workspace = isolated_workspace()
    (workspace / "AGENTS.md").write_text("Repository rules.\n")
    skill = workspace / ".agents" / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo\ndescription: Demo.\n---\nBody stays on disk.\n")
    rows = overhead_rows(request_parameters(workspace)["parameters"], window=200_000)
    labels = [label.strip() for label, _ in rows]
    values = dict((label.strip(), value) for label, value in rows)
    assert labels[0] == "Prompt overhead"
    assert "Instructions" in labels and "Tool schemas" in labels
    assert "AGENTS.md" in labels
    # The skill costs a path in the prompt; its body is read by tool when invoked.
    assert values["Assistant config"].endswith("1 skill")
    assert "% of 200k" in rows[0][1]
    assert "tools" in values["Tool schemas"]


def test_the_request_hook_publishes_parameters_for_the_overview():
    """/context reads the last request, so the hook has to record it before returning."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from pydantic_ai.models import ModelRequestParameters

    from pcode.compaction import AutoCompaction

    parameters = ModelRequestParameters(instruction_parts=[part("planning " * 10, "planning")])
    runtime = SimpleNamespace(
        history=[], session=object(), context_history=None, request_parameters=None
    )
    request = SimpleNamespace(
        messages=[], model="unknown-provider:example", model_request_parameters=parameters
    )
    with (
        patch("pcode.model_metadata.refresh_context"),
        patch("pcode.compaction.effective_window", return_value=None),
    ):
        asyncio.run(AutoCompaction(runtime, "run").before_model_request(None, request))
    assert runtime.request_parameters is parameters
    assert overhead_rows(parameters)[0][0] == "Prompt overhead"


def test_tool_rows_size_every_declared_tool():
    @dataclass
    class Parameters:
        function_tools: list
        output_tools: list

    tools = [
        ToolDefinition(name="small", description="x", parameters_json_schema={}),
        ToolDefinition(name="large", description="y" * 400, parameters_json_schema={}),
    ]
    rows = tool_rows(Parameters(tools, []))
    assert [row.label for row in rows] == ["large", "small"]
    assert rows[0].tokens >= 100
