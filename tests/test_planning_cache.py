"""Planning leaves message history alone; tools expose the stored state."""

import asyncio
import json

import httpx2
import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.openai_codex import OpenAICodexModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai_codex import OpenAICodexCredentials, OpenAICodexProvider
from pydantic_ai_harness.planning import InMemoryPlanStore, PlanItem

from pcode.planning import IdentifiedPlanning


def test_codex_wire_prefix_survives_plan_changes_and_resume():
    async def run():
        bodies = []
        store = InMemoryPlanStore()
        await store.set_items([PlanItem(id="first", content="First task")])

        def handle(request):
            body = json.loads(request.content)
            bodies.append(body)
            n = len(bodies)
            response = {
                "id": f"resp_{n}",
                "created_at": 1,
                "model": body["model"],
                "object": "response",
                "status": "in_progress",
                "output": [],
            }
            item = (
                {
                    "type": "function_call",
                    "id": f"fc_{n}",
                    "call_id": f"call_{n}",
                    "name": "probe",
                    "arguments": "{}",
                    "status": "completed",
                }
                if n < 5
                else {
                    "type": "message",
                    "id": f"msg_{n}",
                    "role": "assistant",
                    "status": "completed",
                    "content": [],
                }
            )
            events = [
                {"type": "response.created", "response": response},
                {"type": "response.output_item.added", "output_index": 0, "item": item},
            ]
            if n >= 5:
                events.append(
                    {
                        "type": "response.output_text.delta",
                        "item_id": item["id"],
                        "output_index": 0,
                        "content_index": 0,
                        "delta": "done",
                    }
                )
            events.append(
                {
                    "type": "response.completed",
                    "response": {
                        **response,
                        "status": "completed",
                        "output": [item],
                        "usage": {"input_tokens": 100, "output_tokens": 1, "total_tokens": 101},
                    },
                }
            )
            return httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content="".join(f"data: {json.dumps(e)}\n\n" for e in events),
            )

        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as http:
            model = OpenAICodexModel(
                "gpt-5.6-sol",
                provider=OpenAICodexProvider(
                    credentials=OpenAICodexCredentials(
                        access_token="test", refresh_token="test", account_id="test"
                    ),
                    http_client=http,
                ),
                profile=OpenAIModelProfile(openai_supports_prompt_cache_breakpoints=False),
            )

            def make_agent():
                agent = Agent(model, capabilities=[IdentifiedPlanning(store=store)])

                @agent.tool_plain
                async def probe() -> str:
                    # First tool leaves the plan unchanged; subsequent ones
                    # update, then clear it. None should inject plan reminders.
                    if len(bodies) == 2:
                        await store.set_items([PlanItem(id="second", content="Changed task")])
                    elif len(bodies) == 3:
                        await store.set_items([])
                    return "result"

                return agent

            async with make_agent() as agent:
                result = await agent.run("Start")
            history = ModelMessagesTypeAdapter.validate_json(
                ModelMessagesTypeAdapter.dump_json(result.all_messages())
            )
            async with make_agent() as resumed:
                await resumed.run("Continue", message_history=history)

        assert len(bodies) == 6
        for before, after in zip(bodies, bodies[1:]):
            assert after["input"][: len(before["input"])] == before["input"]
            assert after["instructions"] == before["instructions"]
            assert after["tools"] == before["tools"]
        text = json.dumps(bodies[-1]["input"])
        assert "<plan-reminder>" not in text
        assert "Changed task" not in text
        assert "prompt_cache_breakpoint" not in json.dumps(bodies)

    asyncio.run(run())


def test_plan_changes_add_no_reminders_across_runs():
    async def run():
        store = InMemoryPlanStore()
        history = []
        prompts = []

        async def model(messages, info):
            assert [
                part.content
                for message in messages
                for part in message.parts
                if isinstance(part, UserPromptPart)
            ] == prompts
            return ModelResponse(parts=[TextPart("ok")])

        for items in (
            [],
            [PlanItem(id="first", content="First task")],
            [PlanItem(id="second", content="Changed task")],
            [],
        ):
            await store.set_items(items)
            prompts.append(f"Turn {len(prompts)}")
            # Recreate the capability and round-trip history as on saved resume.
            agent = Agent(FunctionModel(model), capabilities=[IdentifiedPlanning(store=store)])
            async with agent:
                result = await agent.run(prompts[-1], message_history=history)
            history = ModelMessagesTypeAdapter.validate_json(
                ModelMessagesTypeAdapter.dump_json(result.all_messages())
            )

    asyncio.run(run())


@pytest.mark.parametrize("from_spec", [False, True])
def test_read_plan_recovers_state_without_reminders_or_old_history(from_spec):
    """After history loss, the existing read tool still exposes the store and IDs."""

    async def run():
        store = InMemoryPlanStore()
        await store.set_items([PlanItem(id="first", content="Stored task", status="in_progress")])
        calls = 0

        async def model(messages, info):
            nonlocal calls
            calls += 1
            assert [
                part.content
                for message in messages
                for part in message.parts
                if isinstance(part, UserPromptPart)
            ] == ["Continue"]
            if calls == 1:
                return ModelResponse(parts=[ToolCallPart("read_plan", {}, tool_call_id="read")])
            result = next(part for part in messages[-1].parts if isinstance(part, ToolReturnPart))
            assert "first" in result.content
            assert "Stored task" in result.content
            return ModelResponse(parts=[TextPart("ok")])

        planning = IdentifiedPlanning.from_spec() if from_spec else IdentifiedPlanning()
        planning.store = store
        agent = Agent(FunctionModel(model), capabilities=[planning])
        async with agent:
            await agent.run("Continue")
        assert calls == 2
        assert (await store.get_items())[0].status == "in_progress"

    asyncio.run(run())


def test_legacy_reminders_remain_unchanged_on_resume():
    async def run():
        store = InMemoryPlanStore()
        await store.set_items([PlanItem(id="new", content="New task")])
        history = [
            ModelRequest(parts=[UserPromptPart(content="<plan-reminder>Old task</plan-reminder>")]),
            ModelResponse(parts=[TextPart("ok")]),
        ]
        original = ModelMessagesTypeAdapter.dump_json(history)

        async def model(messages, info):
            assert ModelMessagesTypeAdapter.dump_json(messages[:2]) == original
            assert [
                part.content
                for message in messages[2:]
                for part in message.parts
                if isinstance(part, UserPromptPart)
            ] == ["Continue"]
            return ModelResponse(parts=[TextPart("ok")])

        agent = Agent(FunctionModel(model), capabilities=[IdentifiedPlanning(store=store)])
        async with agent:
            await agent.run("Continue", message_history=history)

    asyncio.run(run())
