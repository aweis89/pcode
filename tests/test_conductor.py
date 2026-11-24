"""Tests for SessionHost peek/ask_aside RPCs and the bundled conductor extension."""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RunUsage

from pcode import remote_profile
from pcode.agent import create_agent
from pcode.aside_guard import AsideGuard
from pcode.ext import BUNDLED_DIR, Extension, ExtensionUI, load_extension
from pcode.host import SessionHost, _Client, _Owner
from pcode.host_protocol import HostEntry, remember_stopped, write_entry
from pcode.live import AgentRuntime
from pcode.remote import request_host
from pcode.rpc import RemoteError
from pcode.sessions import SavedSession


@contextlib.asynccontextmanager
async def _short_host_dir():
    """Unix sockets on macOS cap sun_path at 104 bytes; use a short temp dir."""
    with tempfile.TemporaryDirectory(prefix="pc-", dir="/tmp") as raw:
        directory = Path(raw)
        with patch.dict(os.environ, {"PCODE_HOST_DIR": str(directory)}):
            yield directory


@contextlib.asynccontextmanager
async def _running_host(
    workspace: Path,
    directory: Path,
    *,
    host_id: str = "h1",
    session_id: str = "sess-11111111",
    reply_text: str = "hello from host",
    aside_gate: asyncio.Event | None = None,
    aside_started: asyncio.Event | None = None,
    turn_gate: asyncio.Event | None = None,
    startup_pending: bool = False,
):
    """Spin up an in-process SessionHost over a real Unix socket with a deterministic model."""

    async def responder(messages, info):
        last = messages[-1].parts[-1].content if messages and messages[-1].parts else ""
        if isinstance(last, str) and "side question" in last.lower():
            if aside_started is not None:
                aside_started.set()
            if aside_gate is not None:
                await aside_gate.wait()
            yield f"aside answer: {reply_text}"
            return
        if turn_gate is not None:
            await turn_gate.wait()
        yield reply_text

    model = FunctionModel(stream_function=responder)
    agent = Agent(model)
    runtime = AgentRuntime(agent)

    entry = HostEntry(
        id=host_id,
        pid=os.getpid(),
        model="test:fake",
        workspace=str(workspace),
        session_id=session_id,
        state="idle",
        title="Test host",
        unseen=True,
    )
    host = SessionHost(entry, directory=directory)
    host.controller.model = "test:fake"
    host.controller.workspace = workspace
    host.controller.runtime = runtime
    host.controller.startup_pending = startup_pending
    await host.serve()
    if not startup_pending:
        host.start()
    try:
        yield host, entry
    finally:
        await host.close()


def _load_conductor(tmp_path: Path, *, is_worker: bool = False, session_dir: Path | None = None):
    loaded = load_extension(
        Extension("conductor", BUNDLED_DIR / "conductor.py", "bundled"),
        tmp_path,
        ExtensionUI(),
        session_dir=session_dir or tmp_path / "sessions",
        is_worker=is_worker,
    )
    assert loaded.error is None
    tools = {}
    for cap in loaded.capabilities:
        for tool in getattr(cap, "tools", ()):
            tools[tool.name] = tool.function
    return loaded, tools


def _ctx(conversation_id: str = "parent-00000000", *, agent_name: str | None = None):
    agent = SimpleNamespace(name=agent_name) if agent_name else None
    return SimpleNamespace(conversation_id=conversation_id, agent=agent)


def _conductor_mod(loaded):
    """Return the loaded conductor extension module so patches don't leak globally."""
    import sys

    return sys.modules[loaded.capabilities[0].__class__.__module__]


def test_host_peek_is_read_only_preserves_idle_clock_and_reports_jobs(tmp_path):
    async def run():
        async with _short_host_dir() as directory:
            async with _running_host(tmp_path, directory, host_id="peek1") as (host, entry):
                from pydantic_ai_harness.planning import PlanItem

                await host.controller.runtime.plan_store.set_items(
                    [PlanItem(id="p1", content="Verify peek", status="in_progress")]
                )
                fake_job = SimpleNamespace(
                    id="j1", command="pytest", purpose="run tests", adopted=False
                )
                host.controller.runtime.jobs = SimpleNamespace(running=lambda: [fake_job])
                host.active_at = 123.0

                snap = await request_host(entry, "peek")
                assert snap["id"] == "peek1"
                assert snap["session_id"] == "sess-11111111"
                assert snap["attached"] == 0
                assert snap["unseen"] is True
                assert host.entry.unseen is True  # peek must not clear unseen
                assert len(host.clients) == 0  # peek must not register a client
                assert host.active_at == 123.0  # peek must not reset idle timer
                assert len(snap["plan"]) == 1
                assert snap["plan"][0]["id"] == "p1"
                assert snap["plan"][0]["status"] == "in_progress"
                assert snap["jobs"] == [
                    {"id": "j1", "command": "pytest", "purpose": "run tests", "adopted": False}
                ]
                assert snap["asides"]["total"] == 0
                assert snap["busy_with"] == "a background command is running"

    asyncio.run(run())


def test_host_ask_aside_does_not_wait_for_turn_and_counts_as_busy(tmp_path):
    async def run():
        async with _short_host_dir() as directory:
            turn_gate = asyncio.Event()
            aside_gate = asyncio.Event()
            aside_started = asyncio.Event()
            async with _running_host(
                tmp_path,
                directory,
                host_id="aside1",
                reply_text="42",
                turn_gate=turn_gate,
                aside_gate=aside_gate,
                aside_started=aside_started,
            ) as (host, entry):
                before_history = list(host.controller.runtime.history)

                # Start a gated turn so the host is actively busy.
                await request_host(entry, "send", "long turn")

                # Start ask_aside concurrently; it must begin without waiting for turn_gate.
                aside_task = asyncio.create_task(
                    request_host(entry, "ask_aside", "What is the answer?", "")
                )
                await asyncio.wait_for(aside_started.wait(), timeout=5.0)
                assert host._host_asides == 1

                # While ask_aside is in flight, peek reports it as running.
                snap = await request_host(entry, "peek")
                assert snap["asides"]["running"] == 1

                # Finish the aside while the main turn is still gated.
                aside_gate.set()
                result = await asyncio.wait_for(aside_task, timeout=5.0)
                assert result["host_id"] == "aside1"
                assert result["answer"] == "aside answer: 42"
                assert host._host_asides == 0
                assert host.controller.asides.items == []
                assert host.entry.unseen is True

                # Release the main turn so teardown is clean.
                turn_gate.set()
                # Also verify ask_aside alone blocks stop_if_idle while in flight.
                while host.busy:
                    await asyncio.sleep(0.02)
                aside_gate.clear()
                aside_started.clear()
                aside_task2 = asyncio.create_task(
                    request_host(entry, "ask_aside", "Second question?", "")
                )
                await asyncio.wait_for(aside_started.wait(), timeout=5.0)
                assert host.busy_with() == "a side question is running"
                reason = await request_host(entry, "stop_if_idle", True)
                assert reason == "a side question is running"
                aside_gate.set()
                await asyncio.wait_for(aside_task2, timeout=5.0)
                assert host.controller.asides.items == []
                # The aside never added itself to history (only the main turn did).
                assert len(host.controller.runtime.history) > len(before_history)

    asyncio.run(run())


def test_host_ask_aside_waits_for_startup_and_supports_model_override(tmp_path):
    async def run():
        async with _short_host_dir() as directory:
            async with _running_host(
                tmp_path, directory, host_id="boot1", reply_text="ready", startup_pending=True
            ) as (host, entry):
                override_model = FunctionModel(
                    stream_function=lambda m, i: _single_chunk("override answer")
                )

                async def fake_target_options(target):
                    assert target.model == "custom:fast"
                    assert target.effort == "low"
                    return {"model": SimpleNamespace(model=override_model, settings=None)}

                host.controller.check_effort = lambda target: None
                host.controller.target_options = fake_target_options

                task = asyncio.create_task(
                    request_host(entry, "ask_aside", "Booted yet?", "custom:fast+low")
                )
                await asyncio.sleep(0.05)
                assert not task.done()

                host.controller.startup_pending = False
                host.start()

                result = await asyncio.wait_for(task, timeout=5.0)
                assert result["answer"] == "override answer"
                assert result["model"] == "custom:fast+low"

    asyncio.run(run())


async def _single_chunk(text: str):
    yield text


def test_conductor_worker_scoping_and_aside_guard(tmp_path):
    loaded, parent_tools = _load_conductor(tmp_path, is_worker=False)
    assert set(parent_tools) == {
        "list_hosts",
        "inspect_host",
        "ask_host",
        "send_to_host",
        "wait_for_hosts",
        "stop_host",
    }

    # Isolated worker (is_worker=True) gets only read-only tools.
    _, worker_tools = _load_conductor(tmp_path, is_worker=True)
    assert set(worker_tools) == {"list_hosts", "inspect_host"}

    # Shared worker built by create_agent inherits parent capabilities, so
    # _ConductorCapability.prepare_tools must strip the 4 parent-only tools.
    agent = create_agent("test", tmp_path, extensions=loaded.capabilities)
    cap = loaded.capabilities[0]
    all_defs = [ToolDefinition(name=name) for name in parent_tools]
    parent_ctx = RunContext(deps=None, model=TestModel(), usage=RunUsage(), agent=agent)
    assert {td.name for td in asyncio.run(cap.prepare_tools(parent_ctx, all_defs))} == set(
        parent_tools
    )

    worker_agent = Agent(TestModel(), name="worker")
    worker_ctx = RunContext(deps=None, model=TestModel(), usage=RunUsage(), agent=worker_agent)
    assert {td.name for td in asyncio.run(cap.prepare_tools(worker_ctx, all_defs))} == {
        "list_hosts",
        "inspect_host",
    }

    # Defense-in-depth: direct call from a worker context is refused too.
    async def check_guards():
        for name, args in (
            ("ask_host", ("h1", "hi")),
            ("send_to_host", ("h1", "hi")),
            ("wait_for_hosts", (["h1"],)),
            ("stop_host", ("h1",)),
        ):
            with pytest.raises(ToolFailed, match="not workers"):
                await parent_tools[name](worker_ctx, *args)

        # AsideGuard refuses mutating/recursive conductor tools, allows read-only ones.
        guard = AsideGuard()
        for blocked in ("ask_host", "send_to_host", "wait_for_hosts", "stop_host"):
            with pytest.raises(ToolFailed):
                await guard.wrap_tool_execute(
                    parent_ctx,
                    call=ToolCallPart(tool_name=blocked, args={}),
                    tool_def=ToolDefinition(name=blocked),
                    args={},
                    handler=AsyncMock(return_value="ok"),
                )
        for allowed in ("list_hosts", "inspect_host"):
            handler = AsyncMock(return_value="ok")
            assert (
                await guard.wrap_tool_execute(
                    parent_ctx,
                    call=ToolCallPart(tool_name=allowed, args={}),
                    tool_def=ToolDefinition(name=allowed),
                    args={},
                    handler=handler,
                )
                == "ok"
            )

    asyncio.run(check_guards())

    # Remote profile disables conductor completely.
    profile = remote_profile.RemoteProfile()
    remote_profile.activate(profile)
    try:
        r_loaded, remote_tools = _load_conductor(tmp_path, is_worker=False)
        assert r_loaded.capabilities == []
        assert remote_tools == {}
    finally:
        remote_profile.activate(None)


def test_list_hosts_selectors_exact_match_and_ambiguity(tmp_path):
    async def run():
        async with _short_host_dir() as directory:
            h1 = HostEntry(
                id="h1",
                pid=os.getpid(),
                model="test:fake",
                workspace="/work/async-coder-1",
                session_id="self-session",
                state="working",
                title="Self work",
            )
            h10 = HostEntry(
                id="h10",
                pid=os.getpid(),
                model="test:fake",
                workspace="/work/async-coder-2",
                session_id="other-session",
                state="idle",
                title="Other work",
            )
            stopped1 = HostEntry(
                id="s1",
                pid=os.getpid(),
                model="test:fake",
                workspace="/work/other-repo",
                session_id="stopped-aaaa",
                state="idle",
            )
            stopped2 = HostEntry(
                id="s2",
                pid=os.getpid(),
                model="test:fake",
                workspace="/work/other-repo",
                session_id="stopped-bbbb",
                state="idle",
            )
            write_entry(h1, directory)
            write_entry(h10, directory)
            remember_stopped(stopped1, directory)
            remember_stopped(stopped2, directory)

            loaded, tools = _load_conductor(tmp_path)
            mod = _conductor_mod(loaded)

            # Real RunContext self-detection via conversation_id (with pid patched only in module).
            real_ctx = RunContext(
                deps=None,
                model=TestModel(),
                usage=RunUsage(),
                conversation_id="self-session",
            )
            with patch.object(mod, "_current_pid", return_value=-1):
                res = await tools["list_hosts"](real_ctx, workspace="async-coder")
                assert res["count"] == 2
                by_id = {h["host_id"]: h for h in res["hosts"]}
                assert by_id["h1"]["is_self"] is True
                assert by_id["h10"]["is_self"] is False

                # Exact match beats prefix ambiguity ("h1" vs "h10").
                assert mod._find_running("h1", directory).id == "h1"
                with pytest.raises(ModelRetry, match="multiple running hosts"):
                    mod._find_running("h", directory)

                # Stopped ambiguity is also caught by inspect_host.
                with pytest.raises(ModelRetry, match="multiple stopped sessions"):
                    await tools["inspect_host"](_ctx("unrelated"), "stopped-")

                # Unique stopped match returns helpful summary.
                stopped_info = await tools["inspect_host"](_ctx("unrelated"), "stopped-aaaa")
                assert stopped_info["state"] == "stopped"
                assert "read_session" in stopped_info["note"]

    asyncio.run(run())


def test_conductor_inspect_ask_send_wait_and_stop(tmp_path):
    async def run():
        async with _short_host_dir() as directory:
            gate = asyncio.Event()
            async with _running_host(
                tmp_path,
                directory,
                host_id="h-run",
                session_id="sess-aaaa1111",
                reply_text="all done",
                turn_gate=gate,
            ) as (host, entry):
                loaded, tools = _load_conductor(tmp_path)
                mod = _conductor_mod(loaded)
                with patch.object(mod, "_current_pid", return_value=-1):
                    ctx = _ctx("parent-99999999")

                    # inspect_host
                    snap = await tools["inspect_host"](ctx, "h-run")
                    assert snap["id"] == "h-run"
                    assert snap["is_self"] is False

                    # ask_host
                    ans = await tools["ask_host"](ctx, "h-run", "Status?")
                    assert ans["answer"] == "aside answer: all done"
                    assert ans["resumed"] is False

                    # send_to_host (wait=False) + wait_for_hosts
                    queued = await tools["send_to_host"](ctx, "h-run", "Do the thing", wait=False)
                    assert queued["status"] == "sent"
                    assert queued["attached"] == 0

                    # Unknown selector in wait_for_hosts raises ModelRetry (not fake "stopped").
                    with pytest.raises(ModelRetry, match="No running or recently stopped"):
                        await tools["wait_for_hosts"](ctx, ["typo-id"], mode="any", timeout=1.0)

                    # Wait briefly while gated -> times out cleanly, and stop_host sees busy.
                    timed = await tools["wait_for_hosts"](ctx, ["h-run"], timeout=1.0)
                    assert timed["timed_out"] is True
                    assert len(timed["still_running"]) == 1

                    busy = await tools["stop_host"](ctx, "h-run", force=False)
                    assert busy["status"] == "busy"

                    # Release the turn and wait -> completes.
                    gate.set()
                    done = await tools["wait_for_hosts"](ctx, ["h-run"], mode="any", timeout=10.0)
                    assert done["timed_out"] is False
                    assert len(done["completed"]) == 1
                    assert done["completed"][0]["host_id"] == "h-run"

                    # send_to_host (wait=True) returns reply directly.
                    waited = await tools["send_to_host"](ctx, "h-run", "Second turn", wait=True)
                    assert waited["status"] == "completed"
                    assert waited["reply"] == "all done"

                    # Self-targeting refused across active tools.
                    with pytest.raises(ModelRetry, match="current session"):
                        await tools["ask_host"](_ctx("sess-aaaa1111"), "h-run", "hi")
                    with pytest.raises(ModelRetry, match="current session"):
                        await tools["send_to_host"](_ctx("sess-aaaa1111"), "h-run", "hi")
                    with pytest.raises(ModelRetry, match="current session"):
                        await tools["wait_for_hosts"](_ctx("sess-aaaa1111"), ["h-run"])
                    with pytest.raises(ModelRetry, match="current session"):
                        await tools["stop_host"](_ctx("sess-aaaa1111"), "h-run")

                    # Leading "!" is allowed as a normal queued prompt (never shell mode).
                    bang = await tools["send_to_host"](ctx, "h-run", "!important note", wait=True)
                    assert bang["status"] == "completed"

                    # stop_host refuses atomically on the host when a terminal is attached.
                    with patch.object(host, "terminals", return_value=[object()]):
                        refused = await tools["stop_host"](ctx, "h-run", force=False)
                        assert refused["status"] == "refused"
                        assert "terminal" in refused["reason"]

                    # stop_host(force=False) on idle host preserves the worktree.
                    with patch("pcode.remote.wait_for_exit", new=AsyncMock(return_value=True)):
                        stopped_idle = await tools["stop_host"](ctx, "h-run", force=False)
                        assert stopped_idle["status"] == "stopped"
                        assert host.stopped.is_set()
                        assert host.keep_worktree is True

                    # stop_host(force=True) passes keep_worktree=True.
                    with patch(
                        "pcode.remote.stop_entry", new=AsyncMock(return_value=True)
                    ) as mock_stop:
                        stopped = await tools["stop_host"](ctx, "h-run", force=True)
                        assert stopped["status"] == "stopped"
                        mock_stop.assert_awaited_once_with(entry, keep_worktree=True)

                    # Stopped session returns already_stopped; unknown typo raises ModelRetry.
                    remember_stopped(
                        HostEntry(
                            id="s-done",
                            pid=os.getpid(),
                            model="test:fake",
                            workspace=str(tmp_path),
                            session_id="sess-stopped1",
                            state="idle",
                        ),
                        directory,
                    )
                    already = await tools["stop_host"](ctx, "sess-stopped1")
                    assert already["status"] == "already_stopped"
                    assert already["session_id"] == "sess-stopped1"
                    with pytest.raises(ModelRetry, match="No running or recently stopped"):
                        await tools["stop_host"](ctx, "no-such-host")

    asyncio.run(run())


def test_send_to_host_steers_running_turn_and_waits_for_it(tmp_path):
    async def run():
        async with _short_host_dir() as directory:
            gate = asyncio.Event()
            async with _running_host(
                tmp_path, directory, host_id="h-steer", reply_text="ok", turn_gate=gate
            ) as (host, entry):
                loaded, tools = _load_conductor(tmp_path)
                mod = _conductor_mod(loaded)
                with patch.object(mod, "_current_pid", return_value=-1):
                    ctx = _ctx("parent-99999999")
                    send = tools["send_to_host"]
                    controller = host.controller

                    with pytest.raises(RemoteError, match="mode must be"):
                        await request_host(entry, "send", "nudge", "interrupt")

                    sent = await send(ctx, "h-steer", "long turn")
                    assert sent["status"] == "sent"
                    while not controller.turn_running():
                        await asyncio.sleep(0.02)

                    submitted = []
                    turns = []
                    real_submit, real_turn_ended = controller.submit, controller.turn_ended

                    def record_submit(text, mode, **kwargs):
                        submitted.append((mode, controller.turn_running()))
                        real_submit(text, mode, **kwargs)

                    def record_turn_ended(success):
                        turns.append(success)
                        real_turn_ended(success)

                    controller.submit = record_submit
                    controller.turn_ended = record_turn_ended

                    # Sent mid-turn, it joins that turn rather than queueing behind it,
                    # and wait=True follows the turn it joined to its reply.
                    waited = asyncio.create_task(
                        send(ctx, "h-steer", "focus on tests", wait=True, timeout=10)
                    )
                    while not submitted:
                        await asyncio.sleep(0.02)
                    assert submitted == [("steering", True)]
                    gate.set()
                    result = await asyncio.wait_for(waited, timeout=10.0)
                    assert result["status"] == "completed"
                    assert result["reply"] == "ok"
                    # Only the gated turn ended: the steering message ran no turn of its own.
                    assert turns == [True]
                    controller.submit, controller.turn_ended = real_submit, real_turn_ended
                    texts = [
                        part.content
                        for message in controller.runtime.history
                        for part in message.parts
                        if isinstance(getattr(part, "content", None), str)
                    ]
                    assert any("focus on tests" in text for text in texts)

                    # A host older than the mode argument still takes the prompt, queued.
                    def old_send(client, text):
                        client.become_headless()
                        controller.submit(text, "queue", owner=_Owner(client))

                    with patch.object(_Client, "send", old_send):
                        queued = await send(ctx, "h-steer", "old host, no wait")
                        assert queued["status"] == "sent"
                        old = await send(ctx, "h-steer", "old host", wait=True, timeout=10)
                        assert old["status"] == "completed"

    asyncio.run(run())


def test_conductor_concurrent_auto_resume_spawns_once(tmp_path):
    async def run():
        async with _short_host_dir() as directory:
            session_dir = tmp_path / "sessions"
            ws = tmp_path / "ws"
            ws.mkdir()
            saved = SavedSession.create("test:fake", ws, session_dir)
            sid = saved.info.id
            saved.close()

            loaded, tools = _load_conductor(tmp_path, session_dir=session_dir)
            mod = _conductor_mod(loaded)
            spawn_calls = 0

            def fake_spawn_host(**kwargs):
                nonlocal spawn_calls
                spawn_calls += 1
                identity = "resumed-1"
                entry = HostEntry(
                    id=identity,
                    pid=os.getpid(),
                    model=kwargs["model"],
                    workspace=str(ws),
                    session_id=sid,
                    state="idle",
                )
                write_entry(entry, directory)
                (directory / f"{identity}.sock").touch()
                proc = SimpleNamespace(poll=lambda: None, wait=lambda: 0, returncode=0)
                return identity, proc, directory / f"{identity}.log"

            async def fake_request(entry, method, *args, **kwargs):
                if method == "ask_aside":
                    return {"host_id": entry.id, "session_id": sid, "answer": "ok"}
                return None

            with (
                patch.object(mod, "_current_pid", return_value=-1),
                patch("pcode.remote.spawn_host", side_effect=fake_spawn_host),
                patch("pcode.remote.request_host", side_effect=fake_request),
            ):
                ctx = _ctx("parent-00000000")
                ans, queued = await asyncio.gather(
                    tools["ask_host"](ctx, sid, "Status?"),
                    tools["send_to_host"](ctx, sid, "Continue", wait=False),
                )
                assert spawn_calls == 1
                # Exactly one of the two callers triggered the resume; the other reused it.
                assert sorted([ans["resumed"], queued["resumed"]]) == [False, True]

    asyncio.run(run())


def test_conductor_refuses_open_non_hosted_session_and_missing_workspace(tmp_path):
    async def run():
        async with _short_host_dir():
            session_dir = tmp_path / "sessions"

            # Open in an interactive terminal without a host -> refused cleanly with ToolFailed.
            open_ws = tmp_path / "open-ws"
            open_ws.mkdir()
            open_saved = SavedSession.create("test:fake", open_ws, session_dir)
            try:
                _, tools = _load_conductor(tmp_path, session_dir=session_dir)
                with pytest.raises(ToolFailed, match="interactive terminal"):
                    await tools["ask_host"](_ctx("parent-00000000"), open_saved.info.id, "Hi?")
            finally:
                open_saved.close()

            # Deleted workspace -> points to read_session with ToolFailed.
            gone_ws = tmp_path / "deleted-worktree"
            gone_ws.mkdir()
            gone_saved = SavedSession.create("test:fake", gone_ws, session_dir)
            gone_sid = gone_saved.info.id
            gone_saved.close()
            gone_ws.rmdir()

            _, tools = _load_conductor(tmp_path, session_dir=session_dir)
            with pytest.raises(ToolFailed, match="read_session"):
                await tools["ask_host"](_ctx("parent-00000000"), gone_sid, "Where were you?")

    asyncio.run(run())


def test_conductor_older_host_permission_error_is_friendly(tmp_path):
    async def run():
        async with _short_host_dir() as directory:
            entry = HostEntry(
                id="h-old",
                pid=os.getpid(),
                model="test:fake",
                workspace=str(tmp_path),
                session_id="sess-old11111",
                state="idle",
            )
            write_entry(entry, directory)
            loaded, tools = _load_conductor(tmp_path)
            mod = _conductor_mod(loaded)
            with (
                patch.object(mod, "_current_pid", return_value=-1),
                patch(
                    "pcode.remote.request_host",
                    new=AsyncMock(side_effect=RemoteError("PermissionError", "not allowed")),
                ),
            ):
                with pytest.raises(ToolFailed, match="older pcode"):
                    await tools["inspect_host"](_ctx(), "h-old")

    asyncio.run(run())


def test_conductor_auto_resumes_real_historical_session(tmp_path):
    """End-to-end: send_to_host resumes a saved historical session in its workspace."""

    async def run():
        async with _short_host_dir():
            session_dir = tmp_path / "sessions"
            ws = tmp_path / "ws"
            ws.mkdir()
            saved = SavedSession.create("test", ws, session_dir)
            sid = saved.info.id
            saved.close()

            _, tools = _load_conductor(tmp_path, session_dir=session_dir)
            ctx = _ctx("parent-00000000")
            queued = None
            try:
                queued = await tools["send_to_host"](ctx, sid, "Hello?", wait=False)
                assert queued["resumed"] is True
                assert queued["status"] == "sent"
                assert queued["session_id"] == sid
                assert Path(queued["workspace"]).resolve() == ws.resolve()
                assert "env_note" in queued
                snap = await tools["inspect_host"](ctx, queued["host_id"])
                assert snap["session_id"] == sid
            finally:
                if queued is not None:
                    await tools["stop_host"](ctx, queued["host_id"], force=True)

    asyncio.run(run())


def test_host_ask_aside_validates_effort_and_surfaces_startup_error(tmp_path):
    async def run():
        async with _short_host_dir() as directory:
            async with _running_host(tmp_path, directory, host_id="err1") as (host, entry):
                loaded, tools = _load_conductor(tmp_path)
                mod = _conductor_mod(loaded)
                with patch.object(mod, "_current_pid", return_value=-1):
                    # Invalid +effort is rejected by check_effort -> ModelRetry in ask_host.
                    with pytest.raises(ModelRetry, match="Effort control"):
                        await tools["ask_host"](
                            _ctx("parent-00000000"), "err1", "Status?", model="test:fake+high"
                        )

                    # Startup error surfaces as ToolFailed in ask_host.
                    host.controller.startup_error = RuntimeError("bad api key")
                    with pytest.raises(ToolFailed, match="Agent startup failed"):
                        await tools["ask_host"](_ctx("parent-00000000"), "err1", "Status?")

    asyncio.run(run())


def test_wait_for_hosts_startup_error_older_fallback_and_mid_wait_stop(tmp_path):
    async def run():
        async with _short_host_dir() as directory:
            async with _running_host(tmp_path, directory, host_id="w-err") as (host, entry):
                host.controller.startup_error = RuntimeError("mcp init failed")
                loaded, tools = _load_conductor(tmp_path)
                mod = _conductor_mod(loaded)
                with patch.object(mod, "_current_pid", return_value=-1):
                    res = await tools["wait_for_hosts"](_ctx(), ["w-err"], timeout=2.0)
                    assert res["timed_out"] is False
                    assert len(res["completed"]) == 1
                    assert res["completed"][0]["state"] == "error"
                    assert res["completed"][0]["outcome"] == "failed"
                    assert "RuntimeError" in res["completed"][0]["startup_error"]

            # Older-host fallback: peek raises PermissionError, on-disk entry is idle.
            old = HostEntry(
                id="w-old",
                pid=os.getpid(),
                model="test:fake",
                workspace=str(tmp_path),
                session_id="sess-old22222",
                state="idle",
                outcome="done",
                turns=3,
            )
            write_entry(old, directory)
            loaded, tools = _load_conductor(tmp_path)
            mod = _conductor_mod(loaded)
            with (
                patch.object(mod, "_current_pid", return_value=-1),
                patch(
                    "pcode.remote.request_host",
                    new=AsyncMock(side_effect=RemoteError("PermissionError", "not allowed")),
                ),
            ):
                res = await tools["wait_for_hosts"](_ctx(), ["w-old"], timeout=2.0)
                assert res["timed_out"] is False
                assert res["completed"][0]["host_id"] == "w-old"
                assert res["completed"][0]["turns"] == 3

            # Host disappears mid-wait -> reported as stopped cleanly.
            dying = HostEntry(
                id="w-die",
                pid=os.getpid(),
                model="test:fake",
                workspace=str(tmp_path),
                session_id="sess-die33333",
                state="working",
            )
            write_entry(dying, directory)
            calls = 0

            async def peek_then_vanish(e, method, *args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    (directory / "w-die.json").unlink(missing_ok=True)
                    return {"id": e.id, "session_id": e.session_id, "state": "working"}
                return {}

            with (
                patch.object(mod, "_current_pid", return_value=-1),
                patch("pcode.remote.request_host", side_effect=peek_then_vanish),
            ):
                res = await tools["wait_for_hosts"](_ctx(), ["w-die"], timeout=5.0)
                assert res["timed_out"] is False
                assert res["completed"][0]["state"] == "stopped"
                assert res["completed"][0]["host_id"] == "w-die"

    asyncio.run(run())


def test_send_to_host_wait_timeout_returns_still_running(tmp_path):
    async def run():
        async with _short_host_dir() as directory:
            gate = asyncio.Event()
            async with _running_host(tmp_path, directory, host_id="slow1", turn_gate=gate) as (
                host,
                entry,
            ):
                loaded, tools = _load_conductor(tmp_path)
                mod = _conductor_mod(loaded)
                with patch.object(mod, "_current_pid", return_value=-1):
                    res = await tools["send_to_host"](
                        _ctx(), "slow1", "Take a while", wait=True, timeout=1.0
                    )
                    assert res["status"] == "still_running"
                    assert res["host_id"] == "slow1"
                gate.set()

    asyncio.run(run())


def test_resume_timeout_terminates_child_and_fork_mismatch_stops_host(tmp_path):
    async def run():
        async with _short_host_dir() as directory:
            session_dir = tmp_path / "sessions"
            ws = tmp_path / "ws"
            ws.mkdir()
            saved = SavedSession.create("test:fake", ws, session_dir)
            sid = saved.info.id
            saved.close()

            loaded, _ = _load_conductor(tmp_path, session_dir=session_dir)
            mod = _conductor_mod(loaded)

            # 1) Timeout kills spawned child process and raises ToolFailed.
            terminated = False

            def mark_term():
                nonlocal terminated
                terminated = True

            def spawn_hanging(**kwargs):
                proc = SimpleNamespace(
                    poll=lambda: None,
                    wait=lambda: 0,
                    terminate=mark_term,
                    returncode=0,
                )
                return "hang-1", proc, directory / "hang-1.log"

            with (
                patch.object(mod, "_current_pid", return_value=-1),
                patch("pcode.remote.spawn_host", side_effect=spawn_hanging),
            ):
                with pytest.raises(ToolFailed, match="terminated"):
                    await mod._resume_session_host(
                        sid,
                        session_dir=session_dir,
                        my_session="parent-0",
                        directory=directory,
                        timeout=0.05,
                    )
                assert terminated is True

            # 2) Forked session_id mismatch stops the host and raises ToolFailed.
            def spawn_forked(**kwargs):
                identity = "forked-1"
                entry = HostEntry(
                    id=identity,
                    pid=os.getpid(),
                    model=kwargs["model"],
                    workspace=str(ws),
                    session_id="some-other-forked-session",
                    state="idle",
                )
                write_entry(entry, directory)
                (directory / f"{identity}.sock").touch()
                proc = SimpleNamespace(poll=lambda: None, wait=lambda: 0, returncode=0)
                return identity, proc, directory / f"{identity}.log"

            stop_mock = AsyncMock(return_value=True)
            with (
                patch.object(mod, "_current_pid", return_value=-1),
                patch("pcode.remote.spawn_host", side_effect=spawn_forked),
                patch("pcode.remote.stop_entry", new=stop_mock),
            ):
                with pytest.raises(ToolFailed, match="forked session"):
                    await mod._resume_session_host(
                        sid,
                        session_dir=session_dir,
                        my_session="parent-0",
                        directory=directory,
                        timeout=2.0,
                    )
                stop_mock.assert_awaited_once()

    asyncio.run(run())
