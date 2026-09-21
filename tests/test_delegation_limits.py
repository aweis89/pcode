import asyncio
import json

from pydantic_ai import Agent
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.usage import RunUsage, UsageLimits

from pcode.agent import SUBAGENT_REQUEST_LIMIT, create_agent, create_coder
from pcode.ext import ExtensionAPI, ExtensionUI


def test_child_runs_past_the_parents_near_exhausted_request_budget(tmp_path):
    (tmp_path / "sample.txt").write_text("evidence")
    child_requests = 0

    async def model(messages, info):
        nonlocal child_requests
        if any(tool.name == "delegate_task" for tool in info.function_tools):
            if any(isinstance(part, ToolReturnPart) for msg in messages for part in msg.parts):
                yield "Done"
                return
            yield {
                0: DeltaToolCall(
                    name="delegate_task",
                    json_args=json.dumps({"agent_name": "explorer", "task": "Read"}),
                )
            }
            return
        child_requests += 1
        if child_requests <= 50:
            yield {0: DeltaToolCall(name="read_file", json_args='{"path":"sample.txt"}')}
            return
        yield "Found evidence"

    agent = Agent(FunctionModel(stream_function=model), capabilities=[create_coder(tmp_path)])
    usage = RunUsage(requests=50)
    result = asyncio.run(
        agent.run("Explore", usage=usage, usage_limits=UsageLimits(request_limit=None))
    )
    assert result.output == "Done"
    # The parent starts 50 requests in, which is the library's default cap. The
    # child is unaffected: it runs to its own stopping point.
    assert child_requests == 51
    # Its own budget keeps those requests off the parent's ledger, so a long
    # delegation cannot exhaust the turn. Tokens still aggregate (see
    # tests/test_delegation_cache.py).
    assert result.usage.requests == 52
    assert usage.input_tokens == result.usage.input_tokens


def test_extension_delegate_runs_past_the_parents_spent_request_budget(tmp_path, monkeypatch):
    """A delegate registered without `usage_limits` must not inherit the parent's ledger.

    Regression: the browser delegate shared the parent run's usage counter under
    the library's default 50-request cap, so a long session raised
    `UsageLimitExceeded` inside the delegation. With no budget of its own,
    Harness propagates that instead of steering the parent, aborting the turn.
    """
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    child = Agent(name="helper", description="Helps out", instructions="Help.")

    @child.tool_plain
    def look() -> str:
        """Look at something."""
        return "evidence"

    api = ExtensionAPI("mine", tmp_path, ExtensionUI())
    api.subagent(child)
    (delegate,) = api.subagents
    assert delegate.usage_limits.request_limit == SUBAGENT_REQUEST_LIMIT
    child_requests = 0

    async def model(messages, info):
        nonlocal child_requests
        if any(tool.name == "delegate_task" for tool in info.function_tools):
            if any(isinstance(part, ToolReturnPart) for msg in messages for part in msg.parts):
                yield "Done"
                return
            yield {
                0: DeltaToolCall(
                    name="delegate_task",
                    json_args=json.dumps({"agent_name": "helper", "task": "Look"}),
                )
            }
            return
        child_requests += 1
        if child_requests <= 50:
            yield {0: DeltaToolCall(name="look", json_args="{}")}
            return
        yield "Found evidence"

    agent = create_agent("test", tmp_path, subagents=[delegate])
    usage = RunUsage(requests=50)
    result = asyncio.run(
        agent.run(
            "Explore",
            model=FunctionModel(stream_function=model),
            usage=usage,
            usage_limits=UsageLimits(request_limit=None),
        )
    )
    # The parent starts 50 requests in, which is the library's default cap. The
    # child runs to its own stopping point and the turn survives.
    assert result.output == "Done"
    assert child_requests == 51
