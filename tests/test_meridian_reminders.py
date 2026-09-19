"""Wire-level prefix checks, without upstream requests or credentials."""

import asyncio
import json

import httpx2
import pytest
from anthropic import AsyncAnthropic
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai_harness.planning import InMemoryPlanStore, PlanItem

from pcode.cache_settings import ANTHROPIC_CACHE_SETTINGS
from pcode.meridian_reminders import MeridianLimitWarnings
from pcode.planning import IdentifiedPlanning


@pytest.mark.parametrize("provider_name", ["meridian", "anthropic"])
@pytest.mark.parametrize("plan,warning", [(True, False), (False, True), (True, True)])
def test_wire_prefix_tools_plan_changes_and_saved_resume(plan, warning, provider_name):
    async def run():
        bodies = []
        store = InMemoryPlanStore()
        if plan:
            await store.set_items([PlanItem(id="first", content="First task")])

        def handle(request):
            body = json.loads(request.content)
            bodies.append(body)
            n = len(bodies)
            content = (
                [{"type": "tool_use", "id": f"call_{n}", "name": "probe", "input": {}}]
                if n < 4
                else [{"type": "text", "text": "done"}]
            )
            return httpx2.Response(
                200,
                json={
                    "id": f"msg_{n}",
                    "type": "message",
                    "role": "assistant",
                    "model": body["model"],
                    "content": content,
                    "stop_reason": "tool_use" if n < 4 else "end_turn",
                    "usage": {"input_tokens": 100, "output_tokens": 1},
                },
            )

        class Provider(AnthropicProvider):
            @property
            def name(self):
                return provider_name

        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as http:
            model = AnthropicModel(
                "claude-opus-5",
                provider=Provider(
                    anthropic_client=AsyncAnthropic(api_key="test", http_client=http)
                ),
            )

            def make_agent():
                caps = [IdentifiedPlanning(store=store)]
                if warning:
                    caps.insert(0, MeridianLimitWarnings(max_context_tokens=1))
                return Agent(
                    model,
                    capabilities=caps,
                    model_settings=ANTHROPIC_CACHE_SETTINGS
                    if provider_name == "anthropic"
                    else None,
                )

            agent = make_agent()
            calls = 0

            @agent.tool_plain
            async def probe() -> str:
                nonlocal calls
                calls += 1
                if plan and calls == 1:
                    await store.set_items([PlanItem(id="second", content="Changed task")])
                if plan and calls == 2:
                    await store.set_items([])
                return "result"

            async with agent:
                result = await agent.run("Start")
            # JSON round trip is the saved-session boundary; new capabilities
            # must deduplicate from history, not private process-local counters.
            history = ModelMessagesTypeAdapter.validate_json(
                ModelMessagesTypeAdapter.dump_json(result.all_messages())
            )
            resumed = make_agent()
            async with resumed:
                await resumed.run("Continue", message_history=history)

        assert len(bodies) == 5
        if provider_name == "anthropic" and warning:
            # Limit-warning behavior is unchanged in this planning-only fix;
            # direct Anthropic still uses upstream ephemeral limit warnings.
            assert bodies[1]["messages"][: len(bodies[0]["messages"])] != bodies[0]["messages"]
            return
        if provider_name == "anthropic":
            assert all(
                body["cache_control"] == {"type": "ephemeral", "ttl": "5m"} for body in bodies
            )
        for before, after in zip(bodies, bodies[1:]):
            assert after["messages"][: len(before["messages"])] == before["messages"]
        text = json.dumps(bodies[-1]["messages"])
        if plan:
            assert text.count("<plan-reminder>") == 3
            assert "No active plan." in text
        if warning:
            assert "[WarnNearLimits]" in text

    asyncio.run(run())


def test_warning_deduplicates_deciles_and_preserves_old_messages():
    from types import SimpleNamespace

    from pydantic_ai.messages import ModelRequest, ModelResponse, UserPromptPart
    from pydantic_ai.models import ModelRequestContext, ModelRequestParameters
    from pydantic_ai.usage import RunUsage

    async def run():
        capability = MeridianLimitWarnings(max_iterations=100)
        context = ModelRequestContext(
            model=SimpleNamespace(system="meridian"),
            messages=[ModelRequest(parts=[UserPromptPart(content="start")])],
            model_settings=None,
            model_request_parameters=ModelRequestParameters(),
        )
        ctx = SimpleNamespace(usage=RunUsage(requests=70))
        await capability.before_model_request(ctx, context)
        initial = ModelMessagesTypeAdapter.dump_json(context.messages)
        await capability.before_model_request(ctx, context)
        assert ModelMessagesTypeAdapter.dump_json(context.messages) == initial
        ctx.usage.requests = 71
        await capability.before_model_request(ctx, context)
        assert ModelMessagesTypeAdapter.dump_json(context.messages) == initial
        ctx.usage.requests = 80
        await capability.before_model_request(ctx, context)
        assert len(context.messages) == 3
        assert ModelMessagesTypeAdapter.dump_json(context.messages[:2]) == initial
        # A suspended provider response must not get another user message.
        context.messages.append(ModelResponse(parts=[], state="suspended"))
        ctx.usage.requests = 99
        await capability.before_model_request(ctx, context)
        assert isinstance(context.messages[-1], ModelResponse)

    asyncio.run(run())


def test_coder_installs_meridian_aware_warning(tmp_path):
    from pydantic_ai_harness.compaction import WarnNearLimits

    from pcode.agent import create_coder

    warnings = [c for c in create_coder(tmp_path).capabilities if isinstance(c, WarnNearLimits)]
    assert len(warnings) == 1
    assert isinstance(warnings[0], MeridianLimitWarnings)
    assert warnings[0].max_context_fraction == 0.9
