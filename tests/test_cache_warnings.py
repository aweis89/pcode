"""Real Harness detection, streamed UI delivery, and saved notice replay."""

import asyncio
import warnings
from contextlib import asynccontextmanager
from io import StringIO
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelResponse,
    NativeToolCallPart,
    NativeToolReturnPart,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel as LocalModel
from pydantic_ai.usage import RequestUsage
from pydantic_ai_harness.subagents import SubAgent, SubAgents
from pydantic_ai_harness.warn_on_cache_busts import CacheBustWarning, WarnOnCacheBusts
from rich.console import Console

from pcode.agent import create_coder
from pcode.app import PreviewApp
from pcode.cache_warnings import CacheBustReporting
from pcode.delegation import DelegationReporting, stream_child_activity
from pcode.live import AgentRuntime
from pcode.runtime import CacheBust, Message, TextDelta, ThinkingDelta
from pcode.sessions import SavedSession
from pcode.ui import TerminalOutput


class CacheModel(LocalModel):
    """Stream real responses with prescribed provider cache usage per request."""

    def __init__(self, usages, keys=None):
        super().__init__()
        self.usages = usages
        self.keys = keys
        self.step = 0

    def _request(self, messages, model_settings, model_request_parameters):
        part = (
            TextPart("Done")
            if self.step % len(self.usages) == len(self.usages) - 1
            else ToolCallPart("noop", {})
        )
        return ModelResponse(parts=[part])

    @asynccontextmanager
    async def request_stream(self, *args, **kwargs):
        async with super().request_stream(*args, **kwargs) as response:
            read, write = self.usages[self.step % len(self.usages)]
            if self.keys:
                response._provider_name, response._model_name = self.keys[self.step]
            self.step += 1
            response.usage.cache_read_tokens = read
            response.usage.cache_write_tokens = write
            yield response


def noop():
    return "ok"


def runtime_for(usages, session=None, *, monitor=None):
    return AgentRuntime(
        Agent(
            CacheModel(usages),
            tools=[noop],
            capabilities=[monitor or CacheBustReporting()],
        ),
        session,
    )


async def collect(runtime):
    return [event async for event in runtime.stream("Continue")]


@pytest.mark.parametrize(
    "usages, count",
    [
        ([(0, 8000), (8000, 200), (500, 0)], 1),
        ([(0, 8000), (0, 8000), (0, 8000)], 1),
        ([(8000, 0), (0, 0), (0, 0), (8000, 0), (0, 0)], 2),
        ([(0, 8000), (8000, 200), (8200, 0)], 0),
        ([(0, 0), (0, 0)], 0),
        ([(0, 500), (10, 0)], 0),
        ([(8000, 0), (4000, 0)], 0),
    ],
)
def test_streamed_detector_preserves_threshold_and_latch(usages, count):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        events = asyncio.run(collect(runtime_for(usages)))
    assert not [w for w in caught if issubclass(w.category, CacheBustWarning)]
    busts = [event for event in events if isinstance(event, CacheBust)]
    assert len(busts) == count
    assert Message("Done") in events
    for bust in busts:
        assert bust.text.startswith("Prompt cache: request ")
        assert " reused " in bust.text and " of ~8" in bust.text
        assert "previously cached tokens (test/test)." in bust.text
        assert "cache expired" not in bust.text and "TTL" not in bust.text
        assert "To silence" not in bust.text
        for alarm in ("bust", "warning", "collapse", "fail", "miss"):
            assert alarm not in bust.text.lower()


def reply(read, write, *, server_tool=False):
    parts = [TextPart("x")]
    if server_tool:
        # A server-side tool (web search) runs extra sampling passes inside one
        # API call; the provider reports one usage summed over all of them.
        parts = [
            NativeToolCallPart("web_search", {"query": "x"}, tool_call_id="srv"),
            NativeToolReturnPart("web_search", [], tool_call_id="srv"),
            *parts,
        ]
    return ModelResponse(
        parts=parts,
        usage=RequestUsage(cache_read_tokens=read, cache_write_tokens=write),
        provider_name="test",
        model_name="test",
    )


@pytest.mark.parametrize(
    "last, count",
    [
        # The next request reads the real prefix (8200 + 17000) and must not be
        # judged against the summed figure.
        ((25200, 100), 0),
        # A genuine collapse right after the server-tool step is still caught.
        ((0, 0), 1),
    ],
)
def test_server_tool_usage_does_not_establish_a_prefix(last, count):
    # TestModel cannot stream native tool parts, so drive the hook directly.
    # Five passes over an 8200 prefix report read=5*8200 plus the summed writes.
    replies = [
        reply(0, 8000),
        reply(8000, 200),
        reply(41000, 17000, server_tool=True),
        reply(*last),
    ]
    monitor = CacheBustReporting()
    ctx = MagicMock()
    ctx.emit = AsyncMock()

    async def run():
        for response in replies:
            result = await monitor.after_model_request(
                ctx, request_context=MagicMock(messages=[]), response=response
            )
            assert result is response

    asyncio.run(run())
    busts = [call.args[0] for call in ctx.emit.await_args_list]
    assert len(busts) == count
    for bust in busts:
        assert "of ~8,200 previously cached tokens" in bust.text


def test_turns_and_parallel_runs_have_independent_detectors():
    async def run():
        # Reuse one capability across multiple agents and repeated turns.
        monitor = CacheBustReporting()
        runtimes = [runtime_for([(8000, 0), (0, 0)], monitor=monitor) for _ in range(2)]
        for _ in range(2):
            results = await asyncio.gather(*(collect(runtime) for runtime in runtimes))
            assert all(sum(isinstance(e, CacheBust) for e in events) == 1 for events in results)

    asyncio.run(run())


def test_next_chat_turn_is_compared_with_what_the_last_one_cached():
    async def run():
        runtime = runtime_for([(8000, 0)])
        first = await collect(runtime)
        runtime.agent.model.usages = [(0, 0)]
        second = await collect(runtime)
        assert not any(isinstance(e, CacheBust) for e in first)
        (notice,) = [e for e in second if isinstance(e, CacheBust)]
        assert notice.text == (
            "Prompt cache: request 1 reused 0 of ~8,000 tokens cached in an earlier turn"
            " (test/test)."
        )

    asyncio.run(run())


def test_another_conversation_starts_from_a_clean_mark():
    async def run():
        monitor = CacheBustReporting()
        first = runtime_for([(8000, 0)], monitor=monitor)
        await collect(first)
        second = runtime_for([(0, 0)], monitor=monitor)
        assert not any(isinstance(e, CacheBust) for e in await collect(second))

    asyncio.run(run())


def test_model_switch_starts_own_mark_and_switch_back_keeps_original():
    runtime = AgentRuntime(
        Agent(
            CacheModel(
                [(8000, 0), (0, 0), (0, 0)],
                keys=[("provider", "first"), ("provider", "second"), ("provider", "first")],
            ),
            tools=[noop],
            capabilities=[CacheBustReporting()],
        )
    )
    events = asyncio.run(collect(runtime))
    busts = [e for e in events if isinstance(e, CacheBust)]
    assert len(busts) == 1
    assert busts[0].text.splitlines()[0].endswith("(provider/first).")
    assert "request 3" in busts[0].text


def test_expiry_speculation_is_omitted_even_after_a_long_gap(monkeypatch):
    # Run start, first response, then a response past the ~300s cache TTL.
    times = iter([0, 0, 301])
    monkeypatch.setattr(
        "pydantic_ai_harness.warn_on_cache_busts._capability._now", lambda: next(times)
    )
    events = asyncio.run(collect(runtime_for([(8000, 0), (0, 0)])))
    bust = next(event for event in events if isinstance(event, CacheBust))
    assert "cache TTL" not in bust.text
    assert "cause unknown" in bust.text


@pytest.mark.parametrize("action", ["ignore", "error"])
def test_explicit_warning_filters_still_work(action):
    with warnings.catch_warnings():
        warnings.simplefilter(action, CacheBustWarning)
        if action == "error":
            with pytest.raises(CacheBustWarning):
                asyncio.run(collect(runtime_for([(8000, 0), (0, 0)])))
        else:
            events = asyncio.run(collect(runtime_for([(8000, 0), (0, 0)])))
            assert not any(isinstance(e, CacheBust) for e in events)


def test_unrelated_warnings_are_not_swallowed(monkeypatch):
    original = WarnOnCacheBusts.after_model_request

    async def warn(self, *args, **kwargs):
        warnings.warn("unrelated diagnostic", RuntimeWarning)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(WarnOnCacheBusts, "after_model_request", warn)
    with pytest.warns(RuntimeWarning, match="unrelated diagnostic"):
        asyncio.run(collect(runtime_for([(0, 0)])))


@pytest.mark.parametrize(
    "preferences, count, dumps",
    [
        ({}, 1, False),
        ({"cache_notices": "on", "debug": "on"}, 1, True),
        ({"cache_notices": "off"}, 0, False),
        ({"cache_notices": "off", "debug": "on"}, 0, False),
    ],
)
def test_setting_controls_parent_and_shared_child_notices(
    tmp_path, monkeypatch, preferences, count, dumps
):
    monkeypatch.setattr("pcode.agent.load_preferences", lambda: preferences)
    coder = create_coder(tmp_path)
    children = next(c for c in coder.capabilities if isinstance(c, SubAgents))
    for capabilities in (coder.capabilities, children.shared_capabilities):
        notices = [c for c in capabilities if isinstance(c, CacheBustReporting)]
        assert len(notices) == count
        assert all(notice.dump_fingerprints is dumps for notice in notices)


def test_delegated_notice_reaches_parent_without_child_prose():
    calls = 0

    async def model(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {
                0: DeltaToolCall(
                    name="delegate_task",
                    json_args='{"agent_name":"explorer","task":"Explore"}',
                )
            }
        else:
            yield "Parent answer"

    child = Agent(CacheModel([(8000, 0), (0, 0)]), tools=[noop], name="explorer")
    runtime = AgentRuntime(
        Agent(
            FunctionModel(stream_function=model),
            capabilities=[
                DelegationReporting(),
                SubAgents(
                    agents=[SubAgent(child)],
                    agent_folders=None,
                    shared_capabilities=[CacheBustReporting()],
                    event_stream_handler=stream_child_activity,
                ),
            ],
        )
    )
    events = asyncio.run(collect(runtime))
    busts = [e for e in events if isinstance(e, CacheBust)]
    assert len(busts) == 1 and busts[0].text.startswith("Sub-agent: ")
    assert [e.markdown for e in events if isinstance(e, Message)] == ["Parent answer"]


def replay_text(transcript):
    stream = StringIO()
    console = Console(file=stream, width=80, color_system=None)
    with console.use_theme(transcript.rich_theme):
        for objects, end, soft_wrap in transcript.replay():
            console.print(*objects, end=end, soft_wrap=soft_wrap)
    return stream.getvalue()


def test_notice_survives_saved_session_reopen_and_redraw(tmp_path):
    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    runtime = runtime_for([(8000, 0), (0, 0)], saved)
    try:
        events = asyncio.run(collect(runtime))
        bust = next(e for e in events if isinstance(e, CacheBust))
    finally:
        runtime.close()
    reopened = SavedSession.open(saved.info.id, saved.directory.parent)
    try:
        assert [r["text"] for r in reopened.transcript_records() if r["kind"] == "CacheBust"] == [
            bust.text
        ]
        app = PreviewApp(
            model="test:local",
            runtime=AgentRuntime(runtime.agent, reopened),
            console=Console(file=StringIO(), width=80, color_system=None),
        )
        app.replay()
        for _ in range(2):
            text = replay_text(app.transcript)
            assert text.count("Prompt cache: request 2 reused 0 of ~8,000") == 1
            assert "!" not in text
    finally:
        reopened.close()


@pytest.mark.parametrize("thinking", [False, True])
def test_live_notice_flushes_output_in_order_and_sanitizes(thinking):
    class Runtime:
        session = None

        async def stream(self, prompt):
            yield ThinkingDelta("Before notice") if thinking else TextDelta("Before notice")
            yield CacheBust("Prompt cache: [red]literal[/red]\x1b]0;evil\x07")
            yield TextDelta("After notice")
            yield Message("After notice")

    async def run():
        buffer = StringIO()
        app = PreviewApp(
            model="test:local", runtime=Runtime(), console=Console(file=buffer, color_system=None)
        )
        app.activity.show_thinking = True
        output = TerminalOutput(
            app.transcript.console, MagicMock(), rich_theme=lambda: app.transcript.rich_theme
        )
        output.app.output.get_size.return_value.columns = 80
        app.transcript.output = output
        assert await app.run_live(output, "Question")
        assert buffer.getvalue() == ""
        await output.flush()
        text = buffer.getvalue()
        assert (
            text.index("Before notice") < text.index("Prompt cache:") < text.index("After notice")
        )
        assert text.count("Prompt cache: [red]literal[/red]") == 1
        assert "evil" not in text and "\x1b" not in text and "!" not in text
        assert replay_text(app.transcript).count("Prompt cache:") == 1
        # Muted like other informational notes, not the warning style.
        (notice,) = [
            obj
            for objects, _, _ in app.transcript.replay()
            for obj in objects
            if "Prompt cache:" in getattr(obj, "plain", "")
        ]
        assert notice.style == "pcode.muted"

    asyncio.run(run())
