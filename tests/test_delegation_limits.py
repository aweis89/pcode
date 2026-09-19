import asyncio
import json

from pydantic_ai import Agent
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.usage import RunUsage, UsageLimits

from pcode.agent import create_coder


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
