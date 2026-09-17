import asyncio
import json

from pydantic_ai import Agent
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.usage import RunUsage, UsageLimits

from pcode.agent import create_coder


def test_delegation_exceeds_default_limit_and_preserves_shared_usage(tmp_path):
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
    assert child_requests == 51
    assert result.usage.requests == 103
