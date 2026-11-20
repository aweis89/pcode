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
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.models.function import FunctionModel

from pcode import remote_profile
from pcode.ext import BUNDLED_DIR, Extension, ExtensionUI, load_extension
from pcode.host import SessionHost
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
    turn_gate: asyncio.Event | None = None,
):
    """Spin up an in-process SessionHost over a real Unix socket with a deterministic model."""

    async def responder(messages, info):
        last = messages[-1].parts[-1].content if messages and messages[-1].parts else ""
        if isinstance(last, str) and "side question" in last.lower():
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
    host.controller.startup_pending = False
    await host.serve()
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


def _ctx(conversation_id: str = "parent-00000000"):
    return SimpleNamespace(conversation_id=conversation_id)


def test_host_peek_is_read_only_and_returns_plan_and_asides(tmp_path):
    async def run():
        async with _short_host_dir() as directory:
            async with _running_host(tmp_path, directory, host_id="peek1") as (host, entry):
                from pydantic_ai_harness.planning import PlanItem

                await host.controller.runtime.plan_store.set_items(
                    [PlanItem(id="p1", content="Verify peek", status="in_progress")]
                )
                snap = await request_host(entry, "peek")
                assert snap["id"] == "peek1"
                assert snap["session_id"] == "sess-11111111"
                assert snap["attached"] == 0
                assert snap["unseen"] is True
                assert host.entry.unseen is True  # peek must not clear unseen
                assert len(host.clients) == 0  # peek must not register a client
                assert len(snap["plan"]) == 1
                assert snap["plan"][0]["id"] == "p1"
                assert snap["plan"][0]["content"] == "Verify peek"
                assert snap["plan"][0]["status"] == "in_progress"
                assert snap["asides"]["total"] == 0
                assert snap["busy_with"] == ""

    asyncio.run(run())


def test_host_ask_aside_answers_without_mutating_history_or_asides(tmp_path):
    async def run():
        async with _short_host_dir() as directory:
            async with _running_host(
                tmp_path, directory, host_id="aside1", reply_text="42"
            ) as (host, entry):
                before_history = list(host.controller.runtime.history)
                result = await request_host(entry, "ask_aside", "What is the answer?", "")
                assert result["host_id"] == "aside1"
                assert result["answer"] == "aside answer: 42"
                assert host.controller.runtime.history == before_history
                assert host.controller.asides.items == []
                assert host.entry.unseen is True

    asyncio.run(run())


def test_conductor_worker_and_remote_profile_scoping(tmp_path):
    _, parent_tools = _load_conductor(tmp_path, is_worker=False)
    assert set(parent_tools) == {
        "list_hosts",
        "inspect_host",
        "ask_host",
        "send_to_host",
        "wait_for_hosts",
        "stop_host",
    }

    _, worker_tools = _load_conductor(tmp_path, is_worker=True)
    assert set(worker_tools) == {"list_hosts", "inspect_host"}

    profile = remote_profile.RemoteProfile()
    remote_profile.activate(profile)
    try:
        loaded, remote_tools = _load_conductor(tmp_path, is_worker=False)
        assert loaded.capabilities == []
        assert remote_tools == {}
    finally:
        remote_profile.activate(None)


def test_list_hosts_filters_and_marks_self(tmp_path):
    async def run():
        async with _short_host_dir() as directory:
            live = HostEntry(
                id="h-live",
                pid=os.getpid(),
                model="test:fake",
                workspace="/work/async-coder-1",
                session_id="self-session",
                state="working",
                title="Self work",
            )
            other = HostEntry(
                id="h-other",
                pid=os.getpid(),
                model="test:fake",
                workspace="/work/async-coder-2",
                session_id="other-session",
                state="idle",
                title="Other work",
            )
            stopped = HostEntry(
                id="h-stopped",
                pid=os.getpid(),
                model="test:fake",
                workspace="/work/other-repo",
                session_id="stopped-session",
                state="idle",
                title="Stopped work",
            )
            write_entry(live, directory)
            write_entry(other, directory)
            remember_stopped(stopped, directory)

            _, tools = _load_conductor(tmp_path)
            with patch("os.getpid", return_value=-1):
                res = await tools["list_hosts"](_ctx("self-session"), workspace="async-coder")
                assert res["count"] == 2
                by_id = {h["host_id"]: h for h in res["hosts"]}
                assert by_id["h-live"]["is_self"] is True
                assert by_id["h-other"]["is_self"] is False

                res_stopped = await tools["list_hosts"](_ctx("unrelated"), state="stopped")
                assert [h["session_id"] for h in res_stopped["hosts"]] == ["stopped-session"]

                # inspect_host on a stopped host returns a helpful summary without failing.
                stopped_info = await tools["inspect_host"](_ctx("unrelated"), "stopped-session")
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
                _, tools = _load_conductor(tmp_path)
                with patch("os.getpid", return_value=-1):
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
                    assert queued["status"] == "queued"

                    # Wait briefly while gated -> should time out cleanly.
                    timed = await tools["wait_for_hosts"](ctx, ["h-run"], timeout=1.0)
                    assert timed["timed_out"] is True
                    assert len(timed["still_running"]) == 1

                    # Release the turn and wait -> completes.
                    gate.set()
                    done = await tools["wait_for_hosts"](ctx, ["h-run"], timeout=10.0)
                    assert done["timed_out"] is False
                    assert len(done["completed"]) == 1
                    assert done["completed"][0]["host_id"] == "h-run"

                    # send_to_host (wait=True) returns reply directly.
                    waited = await tools["send_to_host"](ctx, "h-run", "Second turn", wait=True)
                    assert waited["status"] == "completed"
                    assert waited["reply"] == "all done"

                    # Self-targeting and shell mode refused.
                    with pytest.raises(ModelRetry, match="current session"):
                        await tools["send_to_host"](_ctx("sess-aaaa1111"), "h-run", "hi")
                    with pytest.raises(ModelRetry, match="current session"):
                        await tools["stop_host"](_ctx("sess-aaaa1111"), "h-run")
                    with pytest.raises(ModelRetry, match="Shell mode"):
                        await tools["send_to_host"](ctx, "h-run", "!rm -rf /")

                    # stop_host refuses when an attached terminal is present unless force=True.
                    with patch.object(host, "terminals", return_value=[object()]):
                        refused = await tools["stop_host"](ctx, "h-run", force=False)
                        assert refused["status"] == "refused"
                        assert "terminal" in refused["reason"]

                    # stop_host(force=True) passes keep_worktree=True.
                    with patch(
                        "pcode.remote.stop_entry", new=AsyncMock(return_value=True)
                    ) as mock_stop:
                        stopped = await tools["stop_host"](ctx, "h-run", force=True)
                        assert stopped["status"] == "stopped"
                        mock_stop.assert_awaited_once_with(entry, keep_worktree=True)

                    # Already stopped host returns already_stopped cleanly.
                    gone = await tools["stop_host"](ctx, "no-such-host")
                    assert gone["status"] == "already_stopped"

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
            _, tools = _load_conductor(tmp_path)
            with (
                patch("os.getpid", return_value=-1),
                patch(
                    "pcode.remote.request_host",
                    new=AsyncMock(side_effect=RemoteError("PermissionError", "not allowed")),
                ),
            ):
                with pytest.raises(ModelRetry, match="older pcode"):
                    await tools["inspect_host"](_ctx(), "h-old")

    asyncio.run(run())


def test_conductor_missing_workspace_on_resume_points_to_read_session(tmp_path):
    async def run():
        async with _short_host_dir():
            session_dir = tmp_path / "sessions"
            gone_workspace = tmp_path / "deleted-worktree"
            gone_workspace.mkdir()
            saved = SavedSession.create("test:fake", gone_workspace, session_dir)
            sid = saved.info.id
            saved.close()
            gone_workspace.rmdir()

            _, tools = _load_conductor(tmp_path, session_dir=session_dir)
            with pytest.raises(ModelRetry, match="read_session"):
                await tools["ask_host"](_ctx("parent-00000000"), sid, "Where were you?")

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
            queued = await tools["send_to_host"](ctx, sid, "Hello?", wait=False)
            try:
                assert queued["resumed"] is True
                assert queued["status"] == "queued"
                assert queued["session_id"] == sid
                assert Path(queued["workspace"]).resolve() == ws.resolve()
                snap = await tools["inspect_host"](ctx, queued["host_id"])
                assert snap["session_id"] == sid
            finally:
                await tools["stop_host"](ctx, queued["host_id"], force=True)

    asyncio.run(run())
