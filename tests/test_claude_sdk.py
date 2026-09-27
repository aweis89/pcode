"""`claude:` provider over a scripted stand-in for the Agent SDK client.

`FakeCLI` plays the CLI's side of what pcode relies on (verified against the
bundled CLI 2.1.283): raw stream events, one assistant message per content
block, tool calls made through pcode's real in-process MCP server with the
tool_use id in `_meta`, input written mid-turn joining the next request, and
forks through `resume`. Nothing starts a process or bills a request.
"""

import asyncio
import itertools
import json
import time
import weakref
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

import anyio
import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters

import pcode.claude_sdk as claude
from pcode.diagnostics import transient
from pcode.meridian_reminders import append_reminder

MODEL = "claude-test-1"


@dataclass
class World:
    """What every fake process shares: the model's scripted replies, and a log."""

    replies: list = field(default_factory=list)
    clients: list = field(default_factory=list)
    missing: set = field(default_factory=set)
    ids: itertools.count = field(default_factory=lambda: itertools.count(1))
    # Transcript entries only, so tests can name them; stream events use `ids`.
    uuids: itertools.count = field(default_factory=lambda: itertools.count(1))


class FakeCLI:
    def __init__(self, options, world: World) -> None:
        self.options = options
        self.world = world
        world.clients.append(self)
        self.session_id = f"session-{len(world.clients)}"
        self.out: asyncio.Queue = asyncio.Queue()
        self.requests: list[list[dict]] = []  # user content of each API request
        self.queued: list[dict] = []
        self.turn: asyncio.Task | None = None
        self.interrupted = False
        self.disconnected = False
        self.mcp: ClientSession | None = None
        self._stop = asyncio.Event()
        self._serving: asyncio.Task | None = None

    async def connect(self) -> None:
        if self.options.resume in self.world.missing:
            raise RuntimeError(f"No conversation found with session ID: {self.options.resume}")
        prompt = Path(self.options.system_prompt["path"])
        assert prompt.stat().st_mode & 0o777 == 0o600
        self.prompt_file, self.system_prompt = prompt, prompt.read_text()
        ready = asyncio.Event()
        self._serving = asyncio.create_task(self._serve(ready))
        await ready.wait()

    async def _serve(self, ready: asyncio.Event) -> None:
        server = self.options.mcp_servers[claude.SERVER]["instance"]
        async with create_client_server_memory_streams() as (client, served):
            async with anyio.create_task_group() as group:
                group.start_soon(
                    server.run, served[0], served[1], server.create_initialization_options()
                )
                async with ClientSession(client[0], client[1]) as session:
                    await session.initialize()
                    self.mcp = session
                    ready.set()
                    await self._stop.wait()
                group.cancel_scope.cancel()

    async def query(self, prompt) -> None:
        async for message in prompt:
            content = list(message["message"]["content"])
            if self.turn is not None and not self.turn.done():
                self.queued.extend(content)  # the CLI's queue: joins the next request
            else:
                self.turn = asyncio.create_task(self._run(content))

    async def receive_messages(self):
        while (message := await self.out.get()) is not None:
            yield message

    async def interrupt(self) -> None:
        self.interrupted = True
        if self.turn is not None and not self.turn.done():
            self.turn.cancel()
            with suppress(BaseException):
                await self.turn
            self.out.put_nowait(
                ResultMessage("error_during_execution", 1, 1, True, 1, self.session_id)
            )

    async def disconnect(self) -> None:
        self.disconnected = True
        if self.turn is not None and not self.turn.done():
            self.turn.cancel()
        self._stop.set()
        if self._serving is not None:
            with suppress(BaseException):
                await self._serving
        self.out.put_nowait(None)

    # The model's side

    def _uuid(self) -> str:
        return f"uuid-{next(self.world.uuids)}"

    def _event(self, event: dict) -> None:
        uuid = f"event-{next(self.world.ids)}"
        self.out.put_nowait(StreamEvent(uuid, self.session_id, event))

    async def _run(self, content: list[dict]) -> None:
        self.out.put_nowait(SystemMessage("init", {"session_id": self.session_id}))
        while True:
            self.requests.append(content)
            reply = self.world.replies.pop(0)
            if reply[0] == "die":
                self.out.put_nowait(None)
                return
            if reply[0] == "then":
                # A final message, then a request the CLI makes of its own
                # (a nudge after a thinking-only reply, output-limit recovery).
                await self._stream(reply[1])
                await asyncio.sleep(reply[3] if len(reply) > 3 else 0)
                await self._stream(reply[2], pause=5)  # still generating when retired
                self.out.put_nowait(ResultMessage("success", 1, 1, False, 2, self.session_id))
                return
            if reply[0] == "hang":
                await asyncio.Event().wait()
            if reply[0] == "serial":
                # Calls made one at a time; a refused one is answered by the CLI
                # itself, after pcode has sent every result.
                _, _, tools = await self._stream(reply[1], start_calls=False)
                results = []
                for kind, tool_id, name, arguments in tools:
                    if kind == "refused":
                        block = ToolResultBlock(tool_id, "Invalid input", True)
                        self.out.put_nowait(UserMessage([block], uuid=self._uuid()))
                        results.append((tool_id, "Invalid input", True))
                    else:
                        results.append(await self._call(tool_id, name, arguments))
                content = [
                    {"type": "tool_result", "tool_use_id": i, "content": c, "is_error": e}
                    for i, c, e in results
                ]
                continue
            if reply[0] == "error":
                # As the CLI reports an API error (verified): an assistant
                # message naming its kind, then a "success" result flagged
                # is_error, with the HTTP status only when one came back.
                _, kind, status, *text = reply
                text = text[0] if text else "API Error"
                self.out.put_nowait(
                    AssistantMessage([TextBlock(text)], MODEL, error=kind, uuid=self._uuid())
                )
                self.out.put_nowait(
                    ResultMessage(
                        "success",
                        1,
                        1,
                        True,
                        1,
                        self.session_id,
                        api_error_status=status,
                    )
                )
                return
            calls, refused, _ = await self._stream(reply)
            if refused == ["malformed"]:
                # An unparseable tool_use: no handler call, just a retried request.
                await asyncio.sleep(0.05)
                content = [{"type": "text", "text": "[malformed tool use]"}]
                continue
            if refused:
                # The CLI answering a call itself (a refused input) and going on.
                blocks = [ToolResultBlock(i, "Invalid input", True) for i in refused]
                self.out.put_nowait(UserMessage(blocks, uuid=self._uuid()))
                content = [
                    {"type": "tool_result", "tool_use_id": i, "content": "Invalid input"}
                    for i in refused
                ]
                continue
            if not calls:
                self.out.put_nowait(
                    ResultMessage(
                        "success", 1, 1, False, 1, self.session_id, stop_reason="end_turn"
                    )
                )
                return
            results = await asyncio.gather(*calls)
            self.out.put_nowait(
                UserMessage([ToolResultBlock(i, c, e) for i, c, e in results], uuid=self._uuid())
            )
            content = [
                {"type": "tool_result", "tool_use_id": i, "content": c, "is_error": e}
                for i, c, e in results
            ] + self.queued
            self.queued = []

    async def _stream(self, reply, pause: float = 0, start_calls: bool = True):
        self._event(
            {
                "type": "message_start",
                "message": {
                    "id": f"msg_{next(self.world.ids)}",
                    "type": "message",
                    "role": "assistant",
                    "model": MODEL,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 1,
                        "cache_read_input_tokens": 900,
                        "cache_creation_input_tokens": 20,
                    },
                },
            }
        )
        await asyncio.sleep(pause)
        calls, refused, tools = [], [], []
        for index, block in enumerate(reply):
            if block[0] == "text":
                self._event(
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {"type": "text", "text": ""},
                    }
                )
                self._event(
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "text_delta", "text": block[1]},
                    }
                )
                self.out.put_nowait(
                    AssistantMessage([TextBlock(block[1])], MODEL, uuid=self._uuid())
                )
            else:
                kind, name, arguments = block
                tool_id = f"toolu_{next(self.world.ids)}"
                wire = f"{claude.TOOL_PREFIX}{name}"
                self._event(
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": wire,
                            "input": {},
                        },
                    }
                )
                self._event(
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(arguments),
                        },
                    }
                )
                self.out.put_nowait(
                    AssistantMessage(
                        [ToolUseBlock(tool_id, wire, arguments)], MODEL, uuid=self._uuid()
                    )
                )
                tools.append((kind, tool_id, name, arguments))
                if not start_calls:
                    pass
                elif kind == "malformed":
                    refused = ["malformed"]
                elif kind == "refused":
                    refused.append(tool_id)
                else:
                    # Like the CLI: the handler is called when the block ends,
                    # before the message does.
                    calls.append(asyncio.create_task(self._call(tool_id, name, arguments)))
                    await asyncio.sleep(0)
            self._event({"type": "content_block_stop", "index": index})
        stop = "tool_use" if tools else "end_turn"
        self._event(
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": 5},
            }
        )
        self._event({"type": "message_stop"})
        return calls, refused, tools

    async def _call(self, tool_id: str, name: str, arguments: dict):
        result = await self.mcp.call_tool(name, arguments, meta={claude.TOOL_USE_ID: tool_id})
        content = [item.model_dump(mode="json", exclude_none=True) for item in result.content]
        return tool_id, content, bool(result.is_error)


@pytest.fixture(autouse=True)
def plenty_of_memory(monkeypatch):
    """A busy machine must not evict the processes these tests expect to reuse."""
    monkeypatch.setattr(claude, "memory_low", lambda: False)


@pytest.fixture
def world(monkeypatch):
    world = World()
    monkeypatch.setattr(claude, "_client_factory", lambda options: FakeCLI(options, world))
    monkeypatch.setattr(claude, "_pools", weakref.WeakKeyDictionary())
    monkeypatch.setattr(claude, "_index", None)
    return world


def make_agent(**kwargs) -> tuple[Agent, list[str]]:
    agent = Agent(claude.claude_model(f"claude:{MODEL}"), instructions="Be terse.", **kwargs)
    calls: list[str] = []

    @agent.tool_plain
    def lookup(key: str) -> str:
        """Look up a value."""
        calls.append(key)
        return f"value-{key}"

    return agent, calls


def restart() -> None:
    """A new pcode process: no live sessions, the index reloaded from disk."""
    claude._index = None


def texts(content: list[dict]) -> list[str]:
    return [block["text"] for block in content if block.get("type") == "text"]


def run(coroutine_function):
    async def main():
        try:
            return await coroutine_function()
        finally:
            await claude.pool().aclose()

    return asyncio.run(main())


def test_tool_rounds_and_turns_share_one_process(world):
    agent, calls = make_agent()
    world.replies = [
        [("text", "Checking."), ("tool", "lookup", {"key": "a"})],
        [("text", "a is value-a")],
        [("text", "still value-a")],
    ]

    async def main():
        first = await agent.run("look up a")
        second = await agent.run("again?", message_history=first.all_messages())
        return first, second

    first, second = run(main)
    assert (first.output, second.output) == ("a is value-a", "still value-a")
    assert calls == ["a"]
    [cli] = world.clients
    # Each request carries only what the process has not seen.
    assert [texts(r) for r in cli.requests] == [["look up a"], [], ["again?"]]
    assert cli.requests[1][0]["type"] == "tool_result"
    assert cli.requests[1][0]["content"] == [{"type": "text", "text": "value-a"}]
    # pcode's own tool names, not the CLI's MCP names.
    call = next(p for p in first.all_messages()[1].parts if isinstance(p, ToolCallPart))
    assert call.tool_name == "lookup"
    assert first.usage.cache_read_tokens == 1800


def test_process_runs_pcode_prompt_and_tools_only(world):
    agent, _ = make_agent()
    world.replies = [[("text", "ok")]]
    run(lambda: agent.run("hi"))
    options = world.clients[0].options
    assert options.tools == [] and options.setting_sources == []
    assert options.strict_mcp_config and options.allowed_tools == ["mcp__pcode"]
    # Passed as a private file, removed with the process, never on argv.
    assert "Be terse." in world.clients[0].system_prompt
    assert not world.clients[0].prompt_file.exists()
    assert options.model == MODEL and options.include_partial_messages
    assert options.extra_args == {"thinking-display": "summarized"}
    # pcode's Anthropic credentials and endpoint never reach the CLI.
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        assert options.env[name] == ""
    assert options.env["CLAUDE_CODE_MAX_RETRIES"] == "0"
    # No output ceiling from pcode leaves the CLI's own default.
    assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in options.env
    assert options.stderr is not None  # never onto pcode's terminal


def test_output_ceiling_reaches_the_cli(world):
    agent, _ = make_agent()
    world.replies = [[("text", "ok")]]
    run(lambda: agent.run("hi", model_settings={"max_tokens": 64_000}))
    assert world.clients[0].options.env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "64000"


def test_missing_extra_hides_the_provider_and_names_the_fix(monkeypatch):
    from pcode import models

    monkeypatch.setattr(models, "claude_code_configured", lambda: True)
    assert "claude" in models.active_providers(None)
    monkeypatch.setattr(models, "claude_sdk_installed", lambda: False)
    assert "claude" not in models.active_providers(None)
    with pytest.raises(ValueError, match=r"optional `claude` extra") as raised:
        claude.claude_model(f"claude:{MODEL}")
    # Failure messages are never echoed, so the hint is matched by type.
    assert claude.failure_hint(RuntimeError("startup failed")) is None
    wrapped = RuntimeError("startup failed")
    wrapped.__cause__ = raised.value
    assert claude.failure_hint(wrapped) == claude.MISSING_SDK


def test_restart_forks_the_transcript_at_the_last_answer(world):
    agent, _ = make_agent()
    world.replies = [[("tool", "lookup", {"key": "a"})], [("text", "done")], [("text", "again")]]
    first = run(lambda: agent.run("look up a"))
    restart()
    run(lambda: agent.run("and now?", message_history=first.all_messages()))
    old, new = world.clients
    assert new.options.resume == old.session_id and new.options.fork_session
    # Entries: tool_use, tool result, final answer. Fork after the answer.
    assert new.options.resume_session_at == "uuid-3"
    assert new.requests == [[{"type": "text", "text": "and now?"}]]


def test_fork_mid_round_sends_results_structured(world):
    agent, calls = make_agent()
    world.replies = [[("tool", "lookup", {"key": "a"})], [("text", "done")], [("text", "resumed")]]
    first = run(lambda: agent.run("look up a"))
    restart()
    # Resume a history that stops after the tool ran, before the model answered.
    history = first.all_messages()[:3]
    assert isinstance(history[-1].parts[0], ToolReturnPart)
    result = run(lambda: agent.run(message_history=history))
    assert result.output == "resumed"
    new = world.clients[1]
    assert new.options.resume_session_at == "uuid-1"  # the tool_use entry
    [content] = new.requests
    assert [block["type"] for block in content] == ["tool_result"]
    assert calls == ["a"]


def test_foreign_history_is_replayed_as_text(world):
    agent, _ = make_agent()
    history = [
        ModelRequest(parts=[UserPromptPart("look up a")]),
        ModelResponse(parts=[ToolCallPart("lookup", {"key": "a"}, tool_call_id="call_1")]),
        ModelRequest(parts=[ToolReturnPart("lookup", "value-a", tool_call_id="call_1")]),
        ModelResponse(parts=[TextPart("a is value-a")]),
    ]
    world.replies = [[("text", "replayed")]]
    result = run(lambda: agent.run("what was a?", message_history=history))
    assert result.output == "replayed"
    [cli] = world.clients
    assert cli.options.resume is None
    [[block]] = cli.requests
    assert block["text"].startswith(claude.REPLAY_INTRO)
    assert '[Called tool lookup (id call_1) with {"key": "a"}]' in block["text"]
    assert "[Result of tool call call_1]\nvalue-a" in block["text"]
    assert block["text"].endswith("[User]\nwhat was a?\n</conversation>")


def test_missing_transcript_falls_back_to_replay(world):
    agent, _ = make_agent()
    world.replies = [[("text", "one")], [("text", "two")]]
    first = run(lambda: agent.run("hi"))
    restart()
    world.missing.add(world.clients[0].session_id)
    result = run(lambda: agent.run("again", message_history=first.all_messages()))
    assert result.output == "two"
    # The fork is tried, refused, and replaced by a replay.
    assert [c.options.resume for c in world.clients] == [None, "session-1", None]
    assert world.clients[1].requests == []
    assert texts(world.clients[2].requests[0])[0].startswith(claude.REPLAY_INTRO)


def test_input_written_while_parked_joins_the_tool_results(world):
    from pydantic_ai.capabilities import AbstractCapability

    class Reminder(AbstractCapability):
        """Appends a message beside the tool results, as reminders and steering do."""

        async def before_model_request(self, ctx, request_context):
            last = request_context.messages[-1]
            if any(isinstance(p, ToolReturnPart) for p in last.parts):
                append_reminder(request_context, "<steer>", "<steer> say hi")
            return request_context

    agent, _ = make_agent(capabilities=[Reminder()])
    world.replies = [[("tool", "lookup", {"key": "a"})], [("text", "hi")]]
    run(lambda: agent.run("look up a"))
    [cli] = world.clients
    assert [block["type"] for block in cli.requests[1]] == ["tool_result", "text"]
    assert texts(cli.requests[1]) == ["<steer> say hi"]


def test_parallel_calls_are_answered_by_one_request(world):
    agent, calls = make_agent()
    world.replies = [
        [("tool", "lookup", {"key": "a"}), ("tool", "lookup", {"key": "b"})],
        [("text", "both")],
    ]
    result = run(lambda: agent.run("look up a and b"))
    assert result.output == "both"
    assert sorted(calls) == ["a", "b"]
    [cli] = world.clients
    assert [b["type"] for b in cli.requests[1]] == ["tool_result", "tool_result"]


def test_settings_change_moves_to_a_resumed_process(world):
    agent, _ = make_agent()
    world.replies = [[("text", "one")], [("text", "two")]]
    first = run(lambda: agent.run("hi"))

    async def again():
        # Same loop and pool, so only the changed effort can force the new process.
        return await agent.run(
            "again",
            message_history=first.all_messages(),
            model_settings={"anthropic_effort": "high"},
        )

    run(again)
    assert world.clients[1].options.effort == "high"
    assert world.clients[1].options.resume == world.clients[0].session_id


def test_cancelled_stream_interrupts_before_closing(world):
    model = claude.claude_model(f"claude:{MODEL}")
    world.replies = [[("tool", "lookup", {"key": "a"})]]
    messages = [ModelRequest(parts=[UserPromptPart("hi")])]
    parameters = ModelRequestParameters(function_tools=[lookup_definition()])

    async def main():
        async with model.request_stream(messages, None, parameters) as stream:
            async for _ in stream:
                break  # the user pressed Ctrl+C mid-message
        await asyncio.sleep(0.05)
        return list(claude.pool().sessions)  # run() then closes the pool

    assert run(main) == []
    [cli] = world.clients
    assert cli.interrupted and cli.disconnected


def lookup_definition():
    from pydantic_ai.tools import ToolDefinition

    return ToolDefinition(
        name="lookup",
        description="Look up a value.",
        parameters_json_schema={"type": "object", "properties": {"key": {"type": "string"}}},
    )


def test_api_error_is_an_http_error_with_a_login_hint(world):
    agent, _ = make_agent()
    world.replies = [("error", "authentication_failed", 401)]
    with pytest.raises(claude.ClaudeHTTPError) as caught:
        run(lambda: agent.run("hi"))
    assert caught.value.status_code == 401
    assert "/login claude" in claude.failure_hint(caught.value)
    assert not transient(caught.value)


def test_dropped_connection_is_transient_and_the_retry_forks(world):
    agent, _ = make_agent()
    drop = ("error", "server_error", None, "API Error: Connection dropped (ECONNRESET)")
    world.replies = [[("text", "one")], drop, [("text", "three")]]
    first = run(lambda: agent.run("hi"))

    async def twice():
        history = first.all_messages()
        with pytest.raises(claude.ClaudeConnectionError) as caught:
            await agent.run("again", message_history=history)
        assert transient(caught.value)
        assert "no response from Anthropic" in claude.failure_hint(caught.value)
        return await agent.run("again", message_history=history)

    assert run(twice).output == "three"
    # The failed request may sit in that transcript, so the retry forks before it.
    assert world.clients[1].options.resume == world.clients[0].session_id


def test_a_server_error_with_a_status_is_not_retried(world):
    agent, _ = make_agent()
    world.replies = [("error", "server_error", 529, "API Error: 529 Overloaded")]
    with pytest.raises(claude.ClaudeHTTPError) as caught:
        run(lambda: agent.run("hi"))
    # The provider answered: no transport retry, as on pcode's other routes.
    assert caught.value.status_code == 529 and not transient(caught.value)


def test_process_exit_is_transient_and_the_retry_forks(world):
    agent, _ = make_agent()
    world.replies = [[("text", "one")], ("die",), [("text", "three")]]
    first = run(lambda: agent.run("hi"))

    async def twice():
        history = first.all_messages()
        with pytest.raises(claude.ClaudeProcessError) as caught:
            await agent.run("again", message_history=history)
        assert transient(caught.value)
        return await agent.run("again", message_history=history)

    assert run(twice).output == "three"
    # The same (dead) session never serves the retry.
    assert world.clients[2].options.resume == world.clients[0].session_id


def test_cli_answering_a_call_itself_retires_the_session(world):
    agent, calls = make_agent()
    world.replies = [
        [("refused", "lookup", {"key": "a"})],
        [("text", "the stale process carries on")],
        [("text", "after fork")],
    ]
    result = run(lambda: agent.run("look up a"))
    assert result.output == "after fork" and calls == ["a"]
    old, new = world.clients
    # pcode's own result, structured, on a fork at the tool call.
    assert new.options.resume == old.session_id
    assert new.options.resume_session_at == "uuid-1"
    assert new.requests[0][0]["content"] == [{"type": "text", "text": "value-a"}]


def test_idle_processes_expire(world, monkeypatch):
    monkeypatch.setattr(claude, "IDLE_SECONDS", 0.05)
    agent, _ = make_agent()
    world.replies = [[("text", "ok")]]

    async def main():
        await agent.run("hi")
        assert len(claude.pool().sessions) == 1
        await asyncio.sleep(0.2)
        return list(claude.pool().sessions)  # run() then closes the pool

    assert run(main) == []
    assert world.clients[0].disconnected


def test_memory_pressure_stops_idle_processes_between_turns(world, monkeypatch):
    monkeypatch.setattr(claude, "PRESSURE_CHECK_SECONDS", 0.05)
    low = False
    monkeypatch.setattr(claude, "memory_low", lambda: low)
    agent, _ = make_agent()
    world.replies = [[("text", "ok")]]

    async def main():
        nonlocal low
        await agent.run("hi")
        await asyncio.sleep(0.15)
        kept = len(claude.pool().sessions)
        low = True
        # Found by the periodic check: no turn ends to trigger a release.
        await asyncio.sleep(0.15)
        return kept, list(claude.pool().sessions)  # run() then closes the pool

    assert run(main) == (1, [])
    assert world.clients[0].disconnected


def test_memory_pressure_releases_parked_processes_too(monkeypatch):
    monkeypatch.setattr(claude, "memory_low", lambda: True)

    async def main():
        pool = claude.SessionPool(claude.ResumeIndex())
        now = time.monotonic()
        sessions = [StubSession(True, now), StubSession(False, now), StubSession(False, now)]
        sessions[2].busy = True  # mid-request: never touched
        pool.sessions = list(sessions)
        pool.release(sessions[1], ok=True)
        await asyncio.gather(*pool._closing)
        return pool.sessions, sessions

    kept, sessions = asyncio.run(main())
    assert kept == [sessions[2]] and sessions[0].closed and sessions[1].closed


def test_idle_minutes_preference_sets_the_expiry(monkeypatch):
    from pcode.preferences import save_preferences

    assert claude._idle_seconds() == claude.IDLE_SECONDS
    save_preferences(claude_idle_minutes="3")
    assert claude._idle_seconds() == 180


def test_a_message_the_cli_starts_itself_never_answers_pcode(world):
    agent, _ = make_agent()
    world.replies = [
        ("then", [("text", "first")], [("text", "unasked")]),
        [("text", "second")],
    ]

    async def main():
        first = await agent.run("one")
        await asyncio.sleep(0.05)  # the CLI's own request lands meanwhile
        second = await agent.run("two", message_history=first.all_messages())
        return first, second

    first, second = run(main)
    assert (first.output, second.output) == ("first", "second")
    old, new = world.clients
    assert old.interrupted  # stopped rather than left generating
    assert new.options.resume == old.session_id
    assert new.requests == [[{"type": "text", "text": "two"}]]


def test_a_call_the_cli_never_parks_forks_with_pcodes_result(world):
    agent, calls = make_agent()
    world.replies = [
        [("malformed", "lookup", {"key": "a"})],
        [("text", "the retried request, unasked")],
        [("text", "after fork")],
    ]
    result = run(lambda: agent.run("look up a"))
    assert result.output == "after fork" and calls == ["a"]
    old, new = world.clients
    assert new.options.resume == old.session_id
    assert new.requests[0][0]["type"] == "tool_result"


def test_a_request_the_cli_starts_while_pcode_waits_is_cut_short(world):
    agent, _ = make_agent()
    # The CLI's own request starts only after pcode has taken the process again.
    world.replies = [
        ("then", [("text", "first")], [("text", "unasked")], 0.3),
        [("text", "second")],
    ]

    async def main():
        first = await agent.run("one")
        started = time.monotonic()
        second = await agent.run("two", message_history=first.all_messages())
        return second, time.monotonic() - started

    second, elapsed = run(main)
    assert second.output == "second"
    assert elapsed < 3  # not the 5 s the unasked message would have taken
    old, new = world.clients
    assert old.interrupted and new.options.resume == old.session_id


def test_a_later_call_the_cli_refuses_fails_the_request_for_a_retry(world):
    agent, calls = make_agent()
    world.replies = [
        ("serial", [("tool", "lookup", {"key": "a"}), ("refused", "lookup", {"key": "b"})]),
        [("text", "the CLI's own history")],
    ]
    with pytest.raises(claude.ClaudeProcessError) as caught:
        run(lambda: agent.run("look up a and b"))
    # Transient, so the runtime retries, and the retry forks.
    assert transient(caught.value)
    assert sorted(calls) == ["a", "b"]
    # Retired; the fake's own continuation has already ended, so no interrupt.
    assert world.clients[0].disconnected


def test_shutdown_fails_a_waiting_request(world):
    model = claude.claude_model(f"claude:{MODEL}")
    world.replies = [("hang",)]
    messages = [ModelRequest(parts=[UserPromptPart("hi")])]

    async def main():
        async def request():
            async with model.request_stream(messages, None, ModelRequestParameters()) as stream:
                async for _ in stream:
                    pass

        task = asyncio.create_task(request())
        await asyncio.sleep(0.1)
        await claude.shutdown()
        with pytest.raises(claude.ClaudeProcessError, match="stopped"):
            await asyncio.wait_for(task, 2)

    asyncio.run(main())
    assert world.clients[0].disconnected


class StubSession:
    """Just what the pool's bookkeeping reads."""

    def __init__(self, parked: bool, last_used: float) -> None:
        self.busy = self.dead = False
        self.open_tool_ids = ("t",) if parked else ()
        self.last_used = last_used
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(("saved", "kept_finished"), [(None, 1), ("0", 0), ("3", 3)])
def test_parked_sessions_are_kept_and_finished_ones_capped(saved, kept_finished):
    from pcode.preferences import save_preferences

    if saved is not None:
        save_preferences(claude_idle_processes=saved)

    async def main():
        pool = claude.SessionPool(claude.ResumeIndex())
        now = time.monotonic()
        parked = [StubSession(True, now - age) for age in (6, 5, 4)]
        finished = [StubSession(False, now - age) for age in (4, 3, 2, 1)]
        pool.sessions = [*parked, *finished]
        pool.release(finished[-1], ok=True)
        await asyncio.gather(*pool._closing)
        return pool.sessions, parked, finished

    kept, parked, finished = asyncio.run(main())
    # Delegations cannot evict the parent waiting on them.
    assert all(s in kept for s in parked)
    # The oldest finished ones go first.
    closed = len(finished) - kept_finished
    assert all(s.closed for s in finished[:closed])
    assert finished[closed:] == [s for s in kept if not s.open_tool_ids]


def test_shutdown_interrupts_a_parked_turn(world):
    model = claude.claude_model(f"claude:{MODEL}")
    world.replies = [[("tool", "lookup", {"key": "a"})]]
    messages = [ModelRequest(parts=[UserPromptPart("hi")])]
    parameters = ModelRequestParameters(function_tools=[lookup_definition()])

    async def main():
        async with model.request_stream(messages, None, parameters) as stream:
            async for _ in stream:
                pass
        assert len(claude.pool().sessions) == 1  # parked, kept for the result
        await claude.shutdown()

    asyncio.run(main())
    [cli] = world.clients
    assert cli.interrupted and cli.disconnected


def test_history_ending_on_an_answer_is_replayed(world):
    model = claude.claude_model(f"claude:{MODEL}")
    world.replies = [[("text", "continued")]]
    messages = [
        ModelRequest(parts=[UserPromptPart("hi")]),
        ModelResponse(parts=[TextPart("partial")]),
    ]

    async def main():
        async with model.request_stream(messages, None, ModelRequestParameters()) as stream:
            async for _ in stream:
                pass

    run(main)
    [[block]] = world.clients[0].requests
    assert block["text"].endswith("[Assistant]\npartial\n</conversation>")


def test_fallback_workspace_yields_to_the_workers_own(tmp_path):
    from pydantic_ai.models import ModelRequestContext

    model = claude.claude_model(f"claude:{MODEL}")
    parent = claude.ClaudeWorkspace(tmp_path / "parent", fallback=True)
    child = claude.ClaudeWorkspace(tmp_path / "child")

    async def apply(order):
        context = ModelRequestContext(
            model=model, messages=[], model_settings=None, model_request_parameters=None
        )
        for capability in order:
            context = await capability.before_model_request(None, context)
        return context.model_settings[claude.CWD_SETTING]

    child_path = str(tmp_path / "child")
    assert asyncio.run(apply([parent, child])) == child_path
    assert asyncio.run(apply([child, parent])) == child_path
    assert asyncio.run(apply([parent])) == str(tmp_path / "parent")


def test_start_failure_names_the_cli(world, monkeypatch):
    def refuse(options):
        client = FakeCLI(options, world)

        async def connect():
            raise RuntimeError("spawn failed")

        client.connect = connect
        return client

    monkeypatch.setattr(claude, "_client_factory", refuse)
    agent, _ = make_agent()
    with pytest.raises(claude.ClaudeStartError, match="could not start: spawn failed"):
        run(lambda: agent.run("hi"))


def test_workspace_capability_sets_the_cli_directory(world, tmp_path):
    agent, _ = make_agent(capabilities=[claude.ClaudeWorkspace(tmp_path)])
    world.replies = [[("text", "ok")]]
    run(lambda: agent.run("hi"))
    assert world.clients[0].options.cwd == str(tmp_path)


def test_normalize_merges_turns_and_drops_cache_markers():
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "a", "cache_control": {"x": 1}}]},
        {"role": "user", "content": "b"},
        {"role": "assistant", "content": [{"type": "text", "text": "c"}]},
    ]
    assert claude.normalize(messages) == [
        {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "c"}]},
    ]
    chain = claude.lineage(claude.normalize(messages))
    assert len(chain) == 2 and chain != claude.lineage(claude.normalize(messages[1:]))


def test_replay_keeps_open_results_structured_and_answers_missing_ones():
    delta = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "one"},
                {"type": "text", "text": "note"},
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "reply"}]},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "next"},
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": "x"},
                },
            ],
        },
    ]
    content = claude.replay(delta, ("t1", "t2"))
    assert content[0] == {"type": "tool_result", "tool_use_id": "t1", "content": "one"}
    assert content[1]["tool_use_id"] == "t2" and content[1]["is_error"]
    assert "[User]\nnote" in content[2]["text"] and "[Assistant]\nreply" in content[2]["text"]
    assert content[3]["type"] == "image"


def test_resume_index_persists_and_prunes(tmp_path, monkeypatch):
    path = tmp_path / "index.jsonl"
    index = claude.ResumeIndex(path)
    point = claude.ForkPoint("s", "u", "/w")
    index.add("k", point)
    index.add("k", point)  # unchanged entries are not rewritten
    assert len(path.read_text().splitlines()) == 1
    reloaded = claude.ResumeIndex(path)
    assert reloaded.get("k") == point
    monkeypatch.setattr(claude, "INDEX_LIMIT", 4)
    for number in range(6):
        index.add(f"k{number}", point)
    pruned = claude.ResumeIndex(path)
    assert pruned.get("k5") == point and pruned.get("k0") is None
    assert len(path.read_text().splitlines()) == 2
