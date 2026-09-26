import asyncio
import json
import socket
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from pcode.host_protocol import LINE_LIMIT
from pcode.rpc import Peer, RemoteError, RemoteValueError, decode, encode, transportable
from pcode.runtime import EditPreview, JobFinished, PlanUpdated, TextDelta, ToolSummary


@transportable
@dataclass
class Snapshot:
    event: object
    where: Path
    tags: list = field(default_factory=list)


@dataclass
class Unregistered:
    value: int = 0


def round_trip(value):
    """Through real JSON text, as the wire carries it."""
    return decode(json.loads(json.dumps(encode(value))))


# --- codec -------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, True, False, 0, -3, 1.5, "", "text ☃"])
def test_primitives_round_trip(value):
    assert round_trip(value) == value
    assert type(round_trip(value)) is type(value)


def test_nested_containers_round_trip():
    value = {"a": [1, {"b": [None, "c"]}], "d": {}}
    assert round_trip(value) == value
    assert round_trip((1, (2, 3))) == [1, [2, 3]]


def test_path_round_trips():
    assert round_trip(Path("/tmp/x y")) == Path("/tmp/x y")
    assert round_trip({"p": [Path("rel")]}) == {"p": [Path("rel")]}


@pytest.mark.parametrize(
    "event",
    [
        TextDelta("hi"),
        ToolSummary("shell", "ls", failed=True, elapsed_seconds=0.5),
        JobFinished("shell", "sleep", call_id="c1"),
        EditPreview("c2", path="a.py", text="+x", kind="code"),
        PlanUpdated([{"content": "step", "$weird": 1}]),
    ],
)
def test_events_round_trip(event):
    assert round_trip(event) == event
    assert type(round_trip(event)) is type(event)


def test_transportable_dataclass_nests_events_and_paths():
    value = Snapshot(TextDelta("x"), Path("/w"), [Snapshot(EditPreview("c"), Path("p"))])
    decoded = round_trip(value)
    assert decoded == value
    assert decoded.tags[0].where == Path("p")


def test_transportable_decode_drops_unknown_fields():
    data = encode(Snapshot(TextDelta("x"), Path("/w")))
    data["fields"]["newer"] = 1
    assert decode(data) == Snapshot(TextDelta("x"), Path("/w"))


def test_dollar_keys_in_user_dicts_are_not_tags():
    for value in [{"$path": "/x"}, {"$event": 1, "plain": 2}, {"$dict": {"$type": "Snapshot"}}]:
        assert round_trip(value) == value
    assert encode({"plain": 1}) == {"plain": 1}


def test_unsendable_values_raise_type_error():
    with pytest.raises(TypeError, match="str keys"):
        encode({1: "x"})
    with pytest.raises(TypeError, match="Unregistered.*transportable"):
        encode(Unregistered())
    with pytest.raises(TypeError):
        encode({1, 2})
    with pytest.raises(TypeError):
        decode({"$type": "Nope", "fields": {}})


def test_transportable_rejects_name_clashes():
    @dataclass
    class Snapshot:  # noqa: F811 - the clash is the point
        pass

    with pytest.raises(TypeError, match="already named"):
        transportable(Snapshot)
    with pytest.raises(TypeError):
        transportable(int)


# --- peers -------------------------------------------------------------------


async def connect(left_handler, right_handler, *, left_allowed=(), right_allowed=()):
    """Two serving peers joined by a socket pair; each exposes its own handler."""
    a, b = socket.socketpair()
    peers = []
    for sock, handler, allowed in (
        (a, left_handler, left_allowed),
        (b, right_handler, right_allowed),
    ):
        reader, writer = await asyncio.open_connection(sock=sock, limit=LINE_LIMIT)
        peers.append(Peer(reader, writer, handler, allowed=set(allowed)))
    serving = [asyncio.create_task(peer.serve()) for peer in peers]
    return peers[0], peers[1], serving


async def shutdown(*peers):
    for peer in peers:
        peer.close()
    await asyncio.gather(*(peer.wait_closed() for peer in peers))


class Recorder:
    def __init__(self):
        self.seen = []
        self.done = asyncio.Event()
        self.expected = None

    def record(self, *args, **kwargs):
        self.seen.append((args, kwargs))
        if self.expected is not None and len(self.seen) >= self.expected:
            self.done.set()


def test_notifications_arrive_in_order():
    async def scenario():
        recorder = Recorder()
        recorder.expected = 200
        left, right, _ = await connect(None, recorder, right_allowed={"record"})
        for n in range(200):
            left.notify("record", n, event=TextDelta(str(n)))
        await recorder.done.wait()
        assert recorder.seen == [((n,), {"event": TextDelta(str(n))}) for n in range(200)]
        await shutdown(left, right)

    asyncio.run(scenario())


def test_async_notification_handler_is_awaited_before_the_next():
    class Handler:
        def __init__(self):
            self.log = []
            self.release = asyncio.Event()
            self.entered = asyncio.Event()
            self.finished = asyncio.Event()

        async def slow(self):
            self.log.append("slow start")
            self.entered.set()
            await self.release.wait()
            self.log.append("slow end")

        def fast(self):
            self.log.append("fast")
            self.finished.set()

    async def scenario():
        handler = Handler()
        left, right, _ = await connect(None, handler, right_allowed={"slow", "fast"})
        left.notify("slow")
        left.notify("fast")
        await handler.entered.wait()
        # "fast" is already on the wire but must wait for "slow" to finish.
        assert handler.log == ["slow start"]
        handler.release.set()
        await handler.finished.wait()
        assert handler.log == ["slow start", "slow end", "fast"]
        await shutdown(left, right)

    asyncio.run(scenario())


def test_requests_in_both_directions_and_slow_request_does_not_block_notifications():
    class Host:
        def __init__(self):
            self.release = asyncio.Event()
            self.notes = []
            self.noted = asyncio.Event()

        def add(self, a, b=0):
            return a + b

        async def dialog(self, question):
            await self.release.wait()
            return {"answer": question.upper(), "where": Path("/tmp")}

        def note(self, text):
            self.notes.append(text)
            self.noted.set()

    class View:
        async def size(self):
            return (80, 24)

    async def scenario():
        host, view = Host(), View()
        terminal, server, _ = await connect(
            view, host, left_allowed={"size"}, right_allowed={"add", "dialog", "note"}
        )
        assert await terminal.request("add", 2, b=3) == 5
        assert await server.request("size") == [80, 24]
        # Both directions at once.
        results = await asyncio.gather(terminal.request("add", 1), server.request("size"))
        assert results == [1, [80, 24]]

        pending = asyncio.create_task(terminal.request("dialog", "ok?"))
        terminal.notify("note", "after")
        await host.noted.wait()
        assert host.notes == ["after"] and not pending.done()
        host.release.set()
        assert await pending == {"answer": "OK?", "where": Path("/tmp")}
        await shutdown(terminal, server)

    asyncio.run(scenario())


def test_remote_errors():
    class Handler:
        def bad_value(self):
            raise ValueError("no such model: x")

        def bad_lookup(self):
            raise LookupError("missing")

        def bad_value_subclass(self):
            raise UnicodeError("bytes")

        def secret(self):
            raise AssertionError("must not run")

        def unsendable(self):
            return Unregistered()

    async def scenario():
        left, right, _ = await connect(
            None,
            Handler(),
            right_allowed={"bad_value", "bad_lookup", "bad_value_subclass", "unsendable"},
        )
        with pytest.raises(RemoteValueError) as caught:
            await left.request("bad_value")
        assert isinstance(caught.value, ValueError) and isinstance(caught.value, RemoteError)
        assert str(caught.value) == "no such model: x"
        assert caught.value.type_name == "ValueError"
        assert RemoteError.sanitized and RemoteValueError.sanitized

        with pytest.raises(RemoteError) as caught:
            await left.request("bad_lookup")
        assert not isinstance(caught.value, ValueError)
        assert (caught.value.type_name, str(caught.value)) == ("LookupError", "missing")

        with pytest.raises(RemoteValueError) as caught:
            await left.request("bad_value_subclass")
        assert caught.value.type_name == "UnicodeError"

        with pytest.raises(RemoteError) as caught:
            await left.request("secret")
        assert caught.value.type_name == "PermissionError"

        with pytest.raises(RemoteError) as caught:
            await left.request("unsendable")
        assert caught.value.type_name == "TypeError"

        # An unsendable argument fails locally without touching the connection.
        with pytest.raises(TypeError):
            await left.request("bad_lookup", Unregistered())

        await shutdown(left, right)

    asyncio.run(scenario())


def test_failing_or_disallowed_notifications_keep_the_connection(caplog):
    class Handler:
        def __init__(self):
            self.seen = asyncio.Event()

        def boom(self):
            raise RuntimeError("handler bug")

        def secret(self):
            raise AssertionError("must not run")

        def ping(self):
            return "pong"

        def ok(self):
            self.seen.set()

    async def scenario():
        handler = Handler()
        left, right, _ = await connect(None, handler, right_allowed={"boom", "ping", "ok"})
        left.notify("boom")
        left.notify("secret")
        left.notify("ok")
        await handler.seen.wait()
        assert await left.request("ping") == "pong"
        assert not left.closed.is_set() and not right.closed.is_set()
        await shutdown(left, right)

    with caplog.at_level("WARNING", logger="pcode.rpc"):
        asyncio.run(scenario())
    assert "handler bug" in caplog.text
    assert "'secret'" in caplog.text


def test_other_side_closing_fails_pending_requests():
    class Handler:
        def __init__(self):
            self.entered = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def forever(self):
            self.entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    async def scenario():
        handler = Handler()
        left, right, serving = await connect(None, handler, right_allowed={"forever"})
        closes = []
        left.on_close = lambda: closes.append("left")
        right.on_close = lambda: closes.append("right")

        pending = asyncio.create_task(left.request("forever"))
        await handler.entered.wait()
        right.close()
        # The handler's task is cancelled by its own side closing.
        await handler.cancelled.wait()
        with pytest.raises(ConnectionError):
            await pending
        await asyncio.gather(*serving)
        assert left.closed.is_set() and right.closed.is_set()

        left.close()
        right.close()
        assert sorted(closes) == ["left", "right"]
        with pytest.raises(ConnectionError):
            await left.request("forever")
        left.notify("forever")  # silently dropped

    asyncio.run(scenario())


def test_close_flushes_queued_messages():
    async def scenario():
        recorder = Recorder()
        recorder.expected = 50
        left, right, serving = await connect(None, recorder, right_allowed={"record"})
        for n in range(50):
            left.notify("record", n)
        left.close()
        await asyncio.gather(*serving)
        assert [args for args, _ in recorder.seen] == [(n,) for n in range(50)]
        assert right.closed.is_set()

    asyncio.run(scenario())
