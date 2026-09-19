"""Plan updates are durable history, not a mutable request suffix."""

import asyncio
import json
from types import SimpleNamespace

import httpx2
import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestContext, ModelRequestParameters
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
                    # update, then clear it. Old snapshots must remain intact.
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
        assert text.count("<plan-reminder>") == 3
        assert "No active plan." in text
        assert "prompt_cache_breakpoint" not in json.dumps(bodies)

    asyncio.run(run())


def test_unchanged_plan_adds_no_reminder_across_runs():
    """Pydantic AI merges resumed requests and drops application metadata, so
    deduplication must read the sent text, not a marker stored on the message."""

    async def run():
        store = InMemoryPlanStore()
        await store.set_items([PlanItem(id="first", content="First task")])

        async def model(messages, info):
            return ModelResponse(parts=[TextPart("ok")])

        agent = Agent(FunctionModel(model), capabilities=[IdentifiedPlanning(store=store)])

        def reminders(messages):
            return sum(
                content.startswith("<plan-reminder>")
                for message in messages
                for part in message.parts
                if isinstance(content := getattr(part, "content", None), str)
            )

        async with agent:
            history = (await agent.run("one")).all_messages()
        assert reminders(history) == 1
        assert not any(message.metadata for message in history)
        async with agent:
            history = (await agent.run("two", message_history=history)).all_messages()
        assert reminders(history) == 1
        await store.set_items([PlanItem(id="second", content="Changed task")])
        async with agent:
            history = (await agent.run("three", message_history=history)).all_messages()
        assert reminders(history) == 2

    asyncio.run(run())


@pytest.mark.parametrize("inject", [True, False])
def test_plan_deduplication_follows_branch_history(inject):
    async def run():
        store = InMemoryPlanStore()
        capability = IdentifiedPlanning(store=store, inject=inject)
        context = ModelRequestContext(
            model=SimpleNamespace(system="anthropic"),
            messages=[ModelRequest(parts=[UserPromptPart(content="Start")])],
            model_settings=None,
            model_request_parameters=ModelRequestParameters(),
        )
        # Empty plans do not add a reminder to a new session.
        await capability.before_model_request(None, context)
        assert len(context.messages) == 1
        await store.set_items([PlanItem(id="first", content="First task")])
        await capability.before_model_request(None, context)
        first_branch = ModelMessagesTypeAdapter.dump_json(context.messages)
        await capability.before_model_request(None, context)
        assert ModelMessagesTypeAdapter.dump_json(context.messages) == first_branch
        await store.set_items([PlanItem(id="second", content="Changed task")])
        await capability.before_model_request(None, context)
        # Switching back to a branch without the latest snapshot must emit it,
        # even though this capability already emitted it on another branch.
        context.messages = ModelMessagesTypeAdapter.validate_json(first_branch)
        await capability.before_model_request(None, context)
        assert len(context.messages) == (3 if inject else 1)
        if inject:
            assert "Changed task" in context.messages[-1].parts[0].content
        # Clearing all history must not leave a stale process-local dedup key.
        context.messages = [ModelRequest(parts=[UserPromptPart(content="Fresh")])]
        await capability.before_model_request(None, context)
        assert len(context.messages) == (2 if inject else 1)

    asyncio.run(run())
