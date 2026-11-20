"""`antigravity:` models against a scripted `agy acp` (`tests/fake_agy.py`)."""

import asyncio
import json
import sys
from pathlib import Path

import anyio
import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    BinaryContent,
    ModelRequest,
    ModelResponse,
    NativeToolCallPart,
    NativeToolReturnPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from pcode import antigravity
from pcode.antigravity import AntigravityError, AntigravityModel, cached_models, replay_text
from pcode.claude_sdk.workspace import CWD_SETTING


@pytest.fixture
def fake_agy(tmp_path, monkeypatch):
    """Put the fake server on PATH as `agy`; returns a reader for what it received."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "agy"
    fake = Path(__file__).with_name("fake_agy.py")
    script.write_text(f'#!/bin/sh\nexec {sys.executable} -I {fake} "$@"\n')
    script.chmod(0o755)
    log = tmp_path / "agy.jsonl"
    monkeypatch.setenv("FAKE_AGY_LOG", str(log))
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    for name in (
        "GEMINI_API_KEY",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_QUOTA_PROJECT",
        "GOOGLE_GEMINI_BASE_URL",
    ):
        monkeypatch.setenv(name, "synthetic")

    def received(method: str | None = None) -> list[dict]:
        if not log.exists():
            return []
        entries = [json.loads(line) for line in log.read_text().splitlines()]
        return [e for e in entries if method is None or e.get("method") == method]

    return received


def agent_for(model: AntigravityModel, workspace: Path) -> Agent:
    return Agent(model, model_settings={CWD_SETTING: str(workspace)})


def run(coro):
    async def main():
        try:
            return await coro
        finally:
            await antigravity.shutdown()

    return asyncio.run(main())


def prompts(received) -> list[list[dict]]:
    return [e["params"]["prompt"] for e in received("session/prompt")]


def test_one_turn_maps_text_thinking_tools_and_usage(fake_agy, tmp_path):
    async def go():
        return await agent_for(AntigravityModel("fake-low"), tmp_path).run("hello")

    result = run(go())
    response = result.all_messages()[-1]
    kinds = [type(part) for part in response.parts]
    assert kinds == [ThinkingPart, NativeToolCallPart, NativeToolReturnPart, TextPart]
    call, back = response.parts[1], response.parts[2]
    # Antigravity's capitalized arguments, lower-cased for pcode's tool rows.
    assert call.args["path"] == "a.txt" and call.provider_name == "antigravity"
    assert back.outcome == "success" and back.content == "Edited a.txt"
    assert result.output == "got: hello"
    assert response.usage.input_tokens == 13 and response.usage.cache_read_tokens == 3
    assert response.provider_response_id == "s-1#1"
    # Allowed once, with the allow option rather than the first one offered.
    answers = [e for e in fake_agy() if e.get("id") == "perm"]
    assert answers[0]["result"]["outcome"]["optionId"] == "yes"
    # No API key, Cloud project or endpoint: those would bill or route elsewhere.
    assert fake_agy()[0]["env"] == []
    # Background sub-agents can't forward permission requests; see `Connection.start`.
    assert fake_agy()[0]["argv"] == ["acp", "--dangerously-skip-permissions"]
    new = fake_agy("session/new")[0]["params"]
    assert new["cwd"] == str(tmp_path) and new["mcpServers"] == []
    assert fake_agy("session/set_model")[0]["params"]["modelId"] == "fake-low"
    # pcode's own tools are never offered: the agent loop ends after one request.
    assert len(prompts(fake_agy)) == 1
    assert cached_models() == ("fake-high", "fake-low")


def test_follow_up_continues_the_session_and_a_new_model_resumes_it(fake_agy, tmp_path):
    async def go():
        model = AntigravityModel("fake-high")
        first = await agent_for(model, tmp_path).run("one")
        second = await agent_for(model, tmp_path).run("two", message_history=first.all_messages())
        await antigravity.shutdown()
        # As after a restart: a fresh model given the saved history.
        return await agent_for(AntigravityModel("fake-high"), tmp_path).run(
            "three", message_history=second.all_messages()
        )

    third = run(go())
    assert len(fake_agy("session/new")) == 1
    assert [e["params"]["sessionId"] for e in fake_agy("session/resume")] == ["s-1"]
    # Only the new message each time: no replay.
    assert [p for p in prompts(fake_agy)] == [
        [{"type": "text", "text": text}] for text in ("one", "two", "three")
    ]
    assert third.all_messages()[-1].provider_response_id == "s-1#3"


def test_a_rewind_replays_rather_than_resuming_a_session_with_later_turns(fake_agy, tmp_path):
    async def go():
        model = AntigravityModel("fake-high")
        first = await agent_for(model, tmp_path).run("one")
        await agent_for(model, tmp_path).run("two", message_history=first.all_messages())
        # Back to after "one": the session also holds "two", which this history dropped.
        return await agent_for(AntigravityModel("fake-high"), tmp_path).run(
            "branch", message_history=first.all_messages()
        )

    result = run(go())
    assert fake_agy("session/resume") == []
    assert len(fake_agy("session/new")) == 2
    last = prompts(fake_agy)[-1]
    assert "[User]\none" in last[0]["text"] and "two" not in last[0]["text"]
    assert result.all_messages()[-1].provider_response_id == "s-2#1"


def test_another_models_history_is_replayed_tools_included(fake_agy, tmp_path):
    earlier = [
        ModelRequest(parts=[UserPromptPart("remember zebra")]),
        ModelResponse(parts=[ToolCallPart("shell", {"command": "ls"}, "c1")], provider_name="x"),
        ModelRequest(parts=[ToolReturnPart("shell", "a.txt", "c1")]),
        ModelResponse(parts=[TextPart("noted")], provider_name="google"),
    ]

    run(agent_for(AntigravityModel("fake-high"), tmp_path).run("and now?", message_history=earlier))
    assert fake_agy("session/resume") == []
    replayed, latest = prompts(fake_agy)[-1]
    assert "replayed below as a transcript" in replayed["text"]
    assert "[User]\nremember zebra" in replayed["text"]
    assert '[Assistant ran shell: {"command": "ls"}]' in replayed["text"]
    assert "[shell returned: a.txt]" in replayed["text"]
    assert latest == {"type": "text", "text": "and now?"}


@pytest.mark.parametrize(
    "history",
    [
        # A turn running on another model when /model switched to Antigravity.
        [
            ModelRequest(parts=[UserPromptPart("fix it")]),
            ModelResponse(parts=[ToolCallPart("shell", {"command": "ls"}, "c1")]),
            ModelRequest(parts=[ToolReturnPart("shell", "ok", "c1"), UserPromptPart("steer")]),
        ],
        # A continuation with no new message.
        [ModelRequest(parts=[UserPromptPart("fix it")]), ModelResponse(parts=[TextPart("hm")])],
    ],
)
def test_requests_the_user_did_not_send_are_refused_unsent(fake_agy, tmp_path, history):
    model = AntigravityModel("fake-high")
    with pytest.raises(AntigravityError, match="only answers prompts you send"):
        run(model.request(history, None, None))
    assert fake_agy() == []  # Not even a process.


def test_unsupported_attachments_are_refused(fake_agy):
    pdf = BinaryContent(b"%PDF", media_type="application/pdf")
    request = ModelRequest(parts=[UserPromptPart(["read this", pdf])])
    with pytest.raises(AntigravityError, match="text and images here, not BinaryContent"):
        run(AntigravityModel("fake-high").request([request], None, None))


def test_images_are_sent_as_image_blocks(fake_agy, tmp_path):
    png = BinaryContent(b"\x89PNG", media_type="image/png")
    run(agent_for(AntigravityModel("fake-high"), tmp_path).run(["look", png]))
    assert prompts(fake_agy)[0][1] == {"type": "image", "mimeType": "image/png", "data": "iVBORw=="}


def test_never_chooses_always_allow(fake_agy, tmp_path):
    run(agent_for(AntigravityModel("fake-high"), tmp_path).run("ALWAYS"))
    answers = [e for e in fake_agy() if e.get("id") == "perm"]
    assert answers[0]["result"]["outcome"]["optionId"] == "no"


def test_a_refused_turn_leaves_the_session_where_it_was(fake_agy, tmp_path):
    async def go():
        model = AntigravityModel("fake-high")
        first = await agent_for(model, tmp_path).run("one")
        with pytest.raises(AntigravityError, match="agy acp --login"):
            await agent_for(model, tmp_path).run("AUTH", message_history=first.all_messages())
        return await agent_for(model, tmp_path).run("two", message_history=first.all_messages())

    result = run(go())
    assert len(fake_agy("session/new")) == 1
    assert result.all_messages()[-1].provider_response_id == "s-1#2"


def test_a_crash_mid_turn_fails_it_and_the_next_turn_starts_over(fake_agy, tmp_path):
    async def go():
        model = AntigravityModel("fake-high")
        first = await agent_for(model, tmp_path).run("one")
        with pytest.raises(AntigravityError, match="exited"):
            await agent_for(model, tmp_path).run("CRASH", message_history=first.all_messages())
        return await agent_for(model, tmp_path).run("two", message_history=first.all_messages())

    run(go())
    # The crashed session saw a turn this history doesn't have, so it isn't resumed.
    assert fake_agy("session/resume") == []
    assert len(fake_agy("session/new")) == 2
    assert "[User]\none" in prompts(fake_agy)[-1][0]["text"]


def test_a_session_that_cannot_resume_is_replayed(fake_agy, tmp_path):
    antigravity._record_turn("gone-1", 1)
    earlier = [
        ModelRequest(parts=[UserPromptPart("one")]),
        ModelResponse(
            parts=[TextPart("ok")], provider_name="antigravity", provider_response_id="gone-1#1"
        ),
    ]
    run(agent_for(AntigravityModel("fake-high"), tmp_path).run("two", message_history=earlier))
    assert [e["params"]["sessionId"] for e in fake_agy("session/resume")] == ["gone-1"]
    assert "[User]\none" in prompts(fake_agy)[-1][0]["text"]


def test_an_unknown_model_names_the_offered_ones(fake_agy, tmp_path):
    with pytest.raises(AntigravityError, match="Models this account offers: fake-high, fake-low"):
        run(agent_for(AntigravityModel("missing"), tmp_path).run("hi"))


def interrupted(history, prompt: str) -> list:
    """`history` as pcode keeps it after cancelling `prompt`: the request, no reply."""
    from pcode.live import CANCELLED_MARKER

    request = ModelRequest(parts=[UserPromptPart(prompt), UserPromptPart(CANCELLED_MARKER)])
    return [*history, request]


def test_cancel_stops_the_turn_and_the_next_prompt_continues_it(fake_agy, tmp_path):
    async def go():
        model = AntigravityModel("fake-high")
        agent = agent_for(model, tmp_path)
        first = await agent.run("one")
        # Level-triggered, as pcode's runs are cancelled.
        with anyio.move_on_after(1):
            await agent.run("SLOW", message_history=first.all_messages())
        # The next prompt waits for the cancelled turn to really end, then
        # continues the same session, which already saw "SLOW".
        kept = interrupted(first.all_messages(), "SLOW")
        return await agent.run("after", message_history=kept)

    result = run(go())
    assert fake_agy("session/cancel")
    assert result.output == "got: after"
    assert len(fake_agy("session/new")) == 1
    assert prompts(fake_agy)[-1] == [{"type": "text", "text": "after"}]
    assert result.all_messages()[-1].provider_response_id == "s-1#3"


def test_a_rewind_past_a_cancelled_turn_replays(fake_agy, tmp_path):
    async def go():
        model = AntigravityModel("fake-high")
        agent = agent_for(model, tmp_path)
        first = await agent.run("one")
        with anyio.move_on_after(1):
            await agent.run("SLOW", message_history=first.all_messages())
        # Without the cancelled request: the session holds a turn this history lacks.
        return await agent.run("after", message_history=first.all_messages())

    run(go())
    assert len(fake_agy("session/new")) == 2
    assert "[User]\none" in prompts(fake_agy)[-1][0]["text"]


def test_a_new_session_replays_the_interrupted_request_too(fake_agy, tmp_path):
    history = interrupted(
        [
            ModelRequest(parts=[UserPromptPart("one")]),
            ModelResponse(parts=[TextPart("ok")], provider_name="google"),
        ],
        "half-done ask",
    )
    run(agent_for(AntigravityModel("fake-high"), tmp_path).run("after", message_history=history))
    replayed, latest = prompts(fake_agy)[-1]
    assert "[User]\nhalf-done ask" in replayed["text"]
    assert latest == {"type": "text", "text": "after"}


def test_two_cancels_in_a_row_still_continue_the_session(fake_agy, tmp_path):
    async def go():
        model = AntigravityModel("fake-high")
        agent = agent_for(model, tmp_path)
        first = await agent.run("one")
        kept = first.all_messages()
        for ask in ("SLOW a", "SLOW b"):
            with anyio.move_on_after(1):
                await agent.run(ask, message_history=kept)
            kept = interrupted(kept, ask)
        # pcode keeps both requests, merged into one with two markers.
        return await agent.run("after", message_history=kept)

    result = run(go())
    assert len(fake_agy("session/new")) == 1
    assert result.all_messages()[-1].provider_response_id == "s-1#4"


def test_a_cancelled_first_turn_still_continues_the_session(fake_agy, tmp_path):
    async def go():
        agent = agent_for(AntigravityModel("fake-high"), tmp_path)
        with anyio.move_on_after(1):
            await agent.run("SLOW")
        return await agent.run("after", message_history=interrupted([], "SLOW"))

    result = run(go())
    assert len(fake_agy("session/new")) == 1
    assert prompts(fake_agy)[-1] == [{"type": "text", "text": "after"}]
    assert result.all_messages()[-1].provider_response_id == "s-1#2"


def test_a_fork_that_moved_the_session_on_is_not_continued(fake_agy, tmp_path):
    async def go():
        original = AntigravityModel("fake-high")
        first = await agent_for(original, tmp_path).run("one")
        # Another pcode process resumes the same session and adds a turn.
        await agent_for(AntigravityModel("fake-high"), tmp_path).run(
            "fork", message_history=first.all_messages()
        )
        return await agent_for(original, tmp_path).run("mine", message_history=first.all_messages())

    result = run(go())
    assert len(fake_agy("session/new")) == 2
    assert "[User]\none" in prompts(fake_agy)[-1][0]["text"]
    assert result.all_messages()[-1].provider_response_id == "s-2#1"


def test_an_unwritable_state_directory_still_continues_live(fake_agy, tmp_path, monkeypatch):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocked))

    async def go():
        model = AntigravityModel("fake-high")
        first = await agent_for(model, tmp_path).run("one")
        return await agent_for(model, tmp_path).run("two", message_history=first.all_messages())

    run(go())
    assert len(fake_agy("session/new")) == 1


def test_the_ledger_keeps_every_writers_turn(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    writer = (
        "import sys; from pcode import antigravity\n"
        "for n in range(150): antigravity._record_turn(f'{sys.argv[1]}-{n}', n)\n"
    )
    procs = [subprocess.Popen([sys.executable, "-c", writer, tag]) for tag in "abc"]
    assert all(p.wait(60) == 0 for p in procs)
    ledger = json.loads((tmp_path / "pcode" / "antigravity-sessions.json").read_text())
    assert len(ledger) == 450


def test_shutdown_lets_agy_exit_on_its_own(fake_agy, tmp_path):
    async def go():
        model = AntigravityModel("fake-high")
        await agent_for(model, tmp_path).run("one")
        process = model._session.connection.process
        await antigravity.shutdown()
        return process.returncode

    # Closing stdin ends it, not a kill, so agy can save what --continue resumes.
    assert asyncio.run(go()) == 0


def test_replacing_or_closing_the_runtime_stops_its_process(fake_agy, tmp_path):
    from pcode.live import AgentRuntime

    async def stopped(connection) -> bool:
        for _ in range(100):
            if not connection.alive:
                return True
            await asyncio.sleep(0.05)
        return False

    async def go():
        model = AntigravityModel("fake-high")
        runtime = AgentRuntime(agent_for(model, tmp_path))
        await runtime.agent.run("one")
        connection = model._session.connection
        runtime.replace_agent(Agent(AntigravityModel("fake-low")))  # /model
        replaced = await stopped(connection)
        second = runtime.agent.model
        await runtime.agent.run("two", model_settings={CWD_SETTING: str(tmp_path)})
        runtime.close()  # /resume
        return replaced, await stopped(second._session.connection)

    assert run(go()) == (True, True)


def test_continue_is_refused():
    from pcode.live import AgentRuntime
    from pcode.sessions import SessionError

    async def go():
        async for _ in AgentRuntime(Agent(AntigravityModel("fake-high"))).stream(None):
            pass

    with pytest.raises(SessionError, match="/continue isn't available with Antigravity"):
        asyncio.run(go())


def test_malformed_messages_do_not_stop_the_reader(fake_agy, tmp_path):
    async def go():
        model = AntigravityModel("fake-high")
        result = await agent_for(model, tmp_path).run("BADLINE")
        return result.output, model._session.connection.alive

    assert run(go()) == ("got: BADLINE", True)


async def streamed(agent, prompt, history=None):
    """Run as pcode does: streamed, which is what hands the model its run context."""
    from pydantic_ai.run import AgentRunResultEvent

    async with agent.run_stream_events(prompt, message_history=history) as events:
        async for event in events:
            if isinstance(event, AgentRunResultEvent):
                result = event.result
    return result


def steering_agent(model, workspace, queue: list):
    from pcode.steering import STEERING_REMINDER, Steering

    def take():
        taken = list(queue)
        queue.clear()
        return taken

    steering = Steering(take, lambda: bool(queue), reminder=STEERING_REMINDER)
    return Agent(model, model_settings={CWD_SETTING: str(workspace)}, capabilities=[steering])


def test_a_message_sent_mid_turn_steers_it(fake_agy, tmp_path):
    queue: list = []

    async def go():
        agent = steering_agent(AntigravityModel("fake-high"), tmp_path, queue)
        run = asyncio.create_task(streamed(agent, "SLOW"))
        for _ in range(200):
            if prompts(fake_agy):
                break
            await asyncio.sleep(0.02)
        queue.append("also do this")
        return await run

    result = run(go())
    # A second prompt on the same session, which ends the first: agy's steering.
    assert prompts(fake_agy) == [
        [{"type": "text", "text": "SLOW"}],
        [{"type": "text", "text": "also do this"}],
    ]
    response = result.all_messages()[-1]
    texts = [p.content for p in response.parts if isinstance(p, TextPart)]
    assert "".join(texts) == "workinggot: also do this"
    # Both prompts' usage, one turn.
    assert response.usage.output_tokens == 6 and response.provider_response_id == "s-1#1"
    # The history records what Antigravity was told, and no pcode reminder.
    request = result.all_messages()[-2]
    assert [p.content for p in request.parts if isinstance(p, UserPromptPart)] == [
        "SLOW",
        "also do this",
    ]


def test_a_steered_turn_that_is_cancelled_still_continues(fake_agy, tmp_path):
    queue: list = []

    async def go():
        model = AntigravityModel("fake-high")
        agent = steering_agent(model, tmp_path, queue)
        first = await agent.run("one")
        with anyio.move_on_after(1.5):
            run = asyncio.create_task(streamed(agent, "SLOW", first.all_messages()))
            for _ in range(200):
                if len(prompts(fake_agy)) == 2:
                    break
                await asyncio.sleep(0.02)
            queue.append("SLOW too")
            await run
        kept = interrupted(first.all_messages(), "SLOW")
        kept[-1].parts.insert(1, UserPromptPart("SLOW too"))  # Before the marker.
        return await agent.run("after", message_history=kept)

    result = run(go())
    assert len(fake_agy("session/new")) == 1
    assert prompts(fake_agy)[-1] == [{"type": "text", "text": "after"}]
    assert result.output == "got: after"


def test_a_steer_reaches_the_copy_pcode_restores_after_a_cancel(fake_agy, tmp_path):
    from pcode.retries import RequestCheckpoint
    from pcode.steering import Steering

    queue: list = []
    checkpoint = RequestCheckpoint()

    async def go():
        def take():
            taken = list(queue)
            queue.clear()
            return taken

        agent = Agent(
            AntigravityModel("fake-high"),
            model_settings={CWD_SETTING: str(tmp_path)},
            capabilities=[Steering(take, lambda: bool(queue)), checkpoint],
        )
        with anyio.move_on_after(1.5):
            run_ = asyncio.create_task(streamed(agent, "SLOW"))
            for _ in range(200):
                if prompts(fake_agy):
                    break
                await asyncio.sleep(0.02)
            queue.append("SLOW too")
            await run_

    run(go())
    restored = checkpoint.messages[-1]
    assert [p.content for p in restored.parts if isinstance(p, UserPromptPart)] == [
        "SLOW",
        "SLOW too",
    ]


def test_pcode_written_notices_are_kept_from_it():
    from types import SimpleNamespace

    from pcode.background_delegation import AbandonedDelegations
    from pcode.job_notices import JobNotices

    request = ModelRequest(parts=[UserPromptPart("hi")])
    ctx = SimpleNamespace(messages=[request])
    context = SimpleNamespace(model=AntigravityModel("fake-high"), messages=[request])

    class Jobs:
        def take_announcements(self, _):
            raise AssertionError("would take a job notice for Antigravity")

    asyncio.run(JobNotices(Jobs()).before_model_request(ctx, context))
    ids = ["w1"]
    asyncio.run(AbandonedDelegations(ids).before_model_request(ctx, context))
    assert [p.content for p in request.parts] == ["hi"] and ids == ["w1"]


def test_a_finished_job_never_wakes_it(monkeypatch):
    from types import SimpleNamespace

    from pcode.controller import SessionController

    controller = SimpleNamespace(model="antigravity:fake", runtime=SimpleNamespace(jobs=object()))
    assert SessionController.wake_prompt(controller) is None


def test_retiring_drops_unshown_output_after_the_turn():
    shown: list[str] = []

    async def go():
        model = AntigravityModel("fake-high")
        model.listener = shown.append
        model._idle_update({"sessionUpdate": "agent_message_chunk", "content": {"text": "late"}})
        model.retire()
        await asyncio.sleep(antigravity.IDLE_QUIET + 0.2)

    asyncio.run(go())
    assert shown == []


def test_steering_at_a_turns_start_carries_no_pcode_reminder(fake_agy, tmp_path):
    queue = ["queued early"]
    run(streamed(steering_agent(AntigravityModel("fake-high"), tmp_path, queue), "hello"))
    texts = [b["text"] for b in prompts(fake_agy)[0]]
    assert texts == ["hello", "queued early"]


def test_output_after_the_turn_is_shown(fake_agy, tmp_path, monkeypatch):
    monkeypatch.setattr(antigravity, "IDLE_QUIET", 0.1)
    shown: list[str] = []

    async def go():
        model = AntigravityModel("fake-high")
        model.listener = shown.append
        await agent_for(model, tmp_path).run("LATER")
        for _ in range(100):
            if len(shown) == 2:
                break
            await asyncio.sleep(0.05)

    run(go())
    assert shown == [
        "Antigravity, after the turn:\nThe sub-agent found 6 files.",
        "Antigravity, after the turn, ran wc -l",
    ]
    # Shown, never answered: pcode sent no prompt of its own.
    assert len(prompts(fake_agy)) == 1


def test_the_runtime_shows_output_after_the_turn_as_a_lasting_note():
    from pcode.live import AgentRuntime

    model = AntigravityModel("fake-high")
    runtime = AgentRuntime(Agent(model))
    notes: list[str] = []
    runtime.retained_notice = notes.append
    model.listener("hello")
    assert notes == ["hello"]


def test_replay_text_lists_turns_and_tool_calls():
    text = replay_text(
        [
            ModelRequest(parts=[UserPromptPart("fix it")]),
            ModelResponse(
                parts=[NativeToolCallPart("run_command", {"command": "make"}), TextPart("done")]
            ),
        ]
    )
    assert "[User]\nfix it" in text and "[Assistant]\ndone" in text
    assert '[Assistant ran run_command: {"command": "make"}]' in text


def test_side_uses_are_refused():
    from pcode.agent import side_model
    from pcode.compaction import CompactionError, summarize

    with pytest.raises(ValueError, match="only as the conversation's model"):
        side_model("antigravity:fake-high")
    with pytest.raises(CompactionError, match="compacts its own context"):
        asyncio.run(summarize([], model=AntigravityModel("fake-high")))


def test_side_questions_on_the_conversation_model_are_refused():
    from pcode.live import AgentRuntime

    runtime = AgentRuntime(Agent(AntigravityModel("fake-high")))
    with pytest.raises(ValueError, match="only as the conversation's model"):
        asyncio.run(runtime.aside("what changed?"))


def test_provider_shows_when_agy_is_installed(monkeypatch, tmp_path):
    from pcode.models import active_providers, model_catalog

    monkeypatch.setattr(
        "pcode.models.shutil.which", lambda name: "/bin/agy" if name == "agy" else None
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert "antigravity" in active_providers(None)
    # Before any session: one default to pick.
    assert model_catalog({"antigravity"}) == ["antigravity:gemini-4-neon-high"]
    antigravity._remember_models({"models": {"availableModels": [{"modelId": "fake-low"}]}})
    assert model_catalog({"antigravity"}) == ["antigravity:fake-low"]


def test_missing_cli_is_explained(monkeypatch):
    monkeypatch.setattr("pcode.antigravity.shutil.which", lambda _: None)
    with pytest.raises(ValueError, match="not installed"):
        antigravity.antigravity_model("antigravity:fake")


def test_usage_update_records_context_window_and_caches_it(fake_agy, tmp_path):
    from pcode.context_usage import context_label, context_window

    model = AntigravityModel("fake-low")
    assert context_window(model) is None
    assert context_window("antigravity:fake-low") is None

    result = run(agent_for(model, tmp_path).run("hello"))
    assert context_window(model) == 1_048_576
    assert context_window("antigravity:fake-low") == 1_048_576
    assert context_label(model, result.all_messages()) == " · 13/1.05m"

    # A fresh model instance (as after a restart) reloads the cached window from disk.
    antigravity._windows_loaded_path = None
    antigravity._windows = {}
    fresh = AntigravityModel("fake-low")
    assert context_window(fresh) == 1_048_576
    assert context_label(fresh, []) == " · 0/1.05m"


def test_usage_update_used_fallback_when_reply_omits_usage(fake_agy, tmp_path):
    from pcode.context_usage import context_label

    model = AntigravityModel("fake-low")
    result = run(agent_for(model, tmp_path).run("NO_REPLY_USAGE"))
    response = result.all_messages()[-1]
    assert response.usage.input_tokens == 42
    assert context_label(model, result.all_messages()) == " · 42/1.05m"


def test_usage_update_ignores_malformed_and_handles_idle_updates(monkeypatch):
    from pydantic_ai.models import ModelRequestParameters

    from pcode.antigravity import AntigravityStream
    from pcode.context_usage import context_window

    model = AntigravityModel("fake-idle")
    stream = AntigravityStream(model_request_parameters=ModelRequestParameters())
    for bad in (0, -1, True, "1048576", 1.5, None):
        model._idle_update({"sessionUpdate": "usage_update", "size": bad})
    for bad in (-1, True, "42", 1.5, None):
        list(stream._map({"sessionUpdate": "usage_update", "used": bad}))
    assert context_window(model) is None
    assert stream.context_used is None

    model._idle_update({"sessionUpdate": "usage_update", "size": 200_000, "used": 1000})
    assert context_window(model) == 200_000
    monkeypatch.setenv("PCODE_CONTEXT_WINDOW", "100000")
    assert context_window(model) == 100_000


def test_compact_cap_is_ignored_for_antigravity_footer():
    from types import SimpleNamespace

    from pcode.controller import SessionController

    model = AntigravityModel("fake-low")
    model._usage_update({"size": 1_048_576})
    runtime = SimpleNamespace(
        agent=SimpleNamespace(model=model),
        auto_compact=True,
        auto_compact_limit=100_000,
        history=[],
        context_history=None,
    )
    controller = object.__new__(SessionController)
    controller.model = "antigravity:fake-low"
    controller.runtime = runtime
    controller.startup_pending = False
    controller.startup_error = None
    assert controller.compact_cap() is None
    assert controller.autocompact_state() == "managed by Antigravity"
    assert controller.context_label() == " · 0/1.05m"
