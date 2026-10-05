"""Shutdown settles SMTP threads and drains replies before the alias is retired."""

import asyncio
import threading
from collections import Counter
from types import SimpleNamespace

import pytest

from pcode.email_remote.listener import SEND_ATTEMPTS, Listener, Session
from pcode.email_remote.state import State
from pcode.remote_profile import RemoteProfile


def listener_for(tmp_path, send):
    return Listener(
        owner="owner@example.com",
        mailbox=SimpleNamespace(send=send),
        workspace=tmp_path,
        profile=RemoteProfile(),
        ttl=60,
        state_path=tmp_path / "state.json",
        log=lambda _: None,
    )


@pytest.mark.parametrize("first_fails", [False, True])
def test_shutdown_awaits_cancelled_sessions_smtp(tmp_path, first_fails):
    async def go():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        released = threading.Event()
        calls = []

        def send(message):
            calls.append(message["Message-ID"])
            if len(calls) == 1:
                loop.call_soon_threadsafe(entered.set)
                assert released.wait(5), "test did not release SMTP"
                if first_fails:
                    raise OSError("first SMTP attempt failed")

        listener = listener_for(tmp_path, send)
        sending = asyncio.create_task(listener.send("result", subject="result", route="s1"))
        session = Session("s1", "task")
        session.tasks.add(sending)
        listener.sessions[session.key] = session
        shutdown = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            shutdown = asyncio.create_task(listener.shutdown())
            # Shutdown cancels the session before it awaits the retained SMTP.
            with pytest.raises(asyncio.CancelledError):
                await sending
            assert not shutdown.done()
            await listener.retry_outbox(force=True)
            assert len(calls) == 1
            assert listener.state.outbox[0].state == "sending"
        finally:
            released.set()
            if shutdown is not None:
                await asyncio.wait_for(shutdown, 5)
            else:
                await sending
        result, final = State.load(listener.state.path).outbox
        assert result.state == final.state == "sent"
        assert result.attempts == (2 if first_fails else 1)
        assert Counter(calls)[result.planned_rfc_id] == result.attempts
        assert not listener.deliveries

    asyncio.run(go())


def test_cancelled_delivery_waiter_does_not_cancel_smtp(tmp_path):
    async def go():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        calls = []

        def send(message):
            calls.append(message["Message-ID"])
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "test did not release SMTP"

        listener = listener_for(tmp_path, send)
        sending = asyncio.create_task(listener.send("result", subject="result", route="s1"))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            sending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await sending
            entry = listener.state.outbox[0]
            # Even an explicit second delivery joins the existing operation.
            joining = asyncio.create_task(listener.deliver(entry))
        finally:
            release.set()
        await asyncio.wait_for(joining, 5)
        assert len(calls) == 1
        assert State.load(listener.state.path).outbox[0].state == "sent"
        assert not listener.deliveries

    asyncio.run(go())


@pytest.mark.parametrize("permanent", [False, True])
def test_shutdown_drains_existing_and_final_replies(tmp_path, permanent):
    calls = Counter()

    def send(message):
        message_id = message["Message-ID"]
        calls[message_id] += 1
        if permanent or calls[message_id] < 3:
            raise OSError("SMTP unavailable")

    listener = listener_for(tmp_path, send)

    async def go():
        await listener.send("result", subject="result", route="s1")
        assert listener.state.outbox[0].state == "pending"
        await asyncio.wait_for(listener.shutdown(), 5)

    asyncio.run(go())
    entries = State.load(listener.state.path).outbox
    assert len(entries) == 2
    assert entries[-1].subject == "pcode remote: stopped"
    for entry in entries:
        assert entry.state == ("failed" if permanent else "sent")
        assert entry.attempts == (SEND_ATTEMPTS if permanent else 3)
        assert calls[entry.planned_rfc_id] == entry.attempts
    assert not listener.deliveries


def test_shutdown_does_not_exceed_exhausted_attempt_budget(tmp_path):
    calls = []
    listener = listener_for(tmp_path, lambda message: calls.append(message["Message-ID"]))

    async def go():
        await listener.send("result", subject="result", route="s1")
        entry = listener.state.outbox[0]
        entry.state = "pending"
        entry.attempts = SEND_ATTEMPTS
        await listener.shutdown()
        assert entry.state == "failed"
        assert entry.attempts == SEND_ATTEMPTS
        assert calls.count(entry.planned_rfc_id) == 1

    asyncio.run(go())
