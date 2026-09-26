"""A small two-way RPC layer over a session host connection.

Both ends of a host socket are a `Peer`: the terminal calls methods on the
host's session controller, and the host calls methods on each terminal's view.
Messages are the newline-delimited JSON objects of `pcode.host_protocol`:

    {"type": "notify", "method": ..., "args": [...], "kwargs": {...}}
    {"type": "request", "id": n, "method": ..., "args": [...], "kwargs": {...}}
    {"type": "reply", "id": n, "result": ...}
    {"type": "reply", "id": n, "error": {"type": ..., "message": ...}}

Arguments and results go through `encode`/`decode`, which carry plain JSON
data plus runtime events, paths, and dataclasses marked `transportable`.
Tagged values are objects keyed by a `$` name; an ordinary dict that has such
a key of its own is wrapped in `{"$dict": ...}`, so decoding is never a guess.
"""

import asyncio
import dataclasses
import inspect
import itertools
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pcode.host_protocol import EVENT_TYPES, decode_event, dumps, encode_event, read_message

logger = logging.getLogger(__name__)

# Dataclasses registered with `transportable`, by class name.
_TYPES: dict[str, type] = {}


def transportable(cls: type) -> type:
    """Let instances of dataclass `cls` be sent as arguments and results."""
    if not (isinstance(cls, type) and dataclasses.is_dataclass(cls)):
        raise TypeError(f"{cls!r} is not a dataclass.")
    name = cls.__name__
    # Classes travel by bare name, so two of them may not share one.
    if name in EVENT_TYPES or _TYPES.get(name, cls) is not cls:
        raise TypeError(f"Another transportable type is already named {name}.")
    _TYPES[name] = cls
    return cls


def encode(value: Any) -> Any:
    """`value` as JSON-compatible data that `decode` turns back into it."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [encode(item) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("Only dicts with str keys can be sent over RPC.")
        data = {key: encode(item) for key, item in value.items()}
        return {"$dict": data} if any(key.startswith("$") for key in value) else data
    if isinstance(value, Path):
        return {"$path": str(value)}
    cls = type(value)
    # Exact class, not isinstance: JobFinished subclasses ToolSummary, and a
    # subclass nobody registered would come back as its parent.
    if EVENT_TYPES.get(cls.__name__) is cls:
        return {"$event": encode_event(value)}
    if _TYPES.get(cls.__name__) is cls:
        fields = {f.name: encode(getattr(value, f.name)) for f in dataclasses.fields(value)}
        return {"$type": cls.__name__, "fields": fields}
    if dataclasses.is_dataclass(value):
        raise TypeError(
            f"{cls.__qualname__} cannot be sent over RPC; mark it with pcode.rpc.transportable."
        )
    raise TypeError(f"{cls.__qualname__} cannot be sent over RPC.")


def decode(value: Any) -> Any:
    """The Python value `encode` produced `value` from."""
    if isinstance(value, list):
        return [decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    if "$dict" in value:
        return {key: decode(item) for key, item in value["$dict"].items()}
    if "$path" in value:
        return Path(value["$path"])
    if "$event" in value:
        # Event fields are plain JSON already (see encode_event), never tagged.
        return decode_event(value["$event"])
    if "$type" in value:
        name = value["$type"]
        if (cls := _TYPES.get(name)) is None:
            raise TypeError(f"Unknown transportable type {name!r}.")
        data = value["fields"]
        # Fields a newer peer added are dropped, as decode_event does.
        return cls(
            **{
                f.name: decode(data[f.name])
                for f in dataclasses.fields(cls)
                if f.init and f.name in data
            }
        )
    if any(key.startswith("$") for key in value):
        raise TypeError(f"Unknown tagged value {sorted(value)!r}.")
    return {key: decode(item) for key, item in value.items()}


class RemoteError(Exception):
    """The other side's handler raised; `type_name` is its exception class."""

    # The message was already made presentable on the side that raised it.
    sanitized = True

    def __init__(self, type_name: str, message: str):
        super().__init__(message)
        self.type_name = type_name
        self.message = message


class RemoteValueError(RemoteError, ValueError):
    """A remote ValueError, so usage errors are caught like local ones."""


def _remote_error(error: dict) -> RemoteError:
    type_name = str(error.get("type", "Exception"))
    message = str(error.get("message", ""))
    cls = RemoteValueError if error.get("value_error") else RemoteError
    return cls(type_name, message)


class Peer:
    """One end of an RPC connection.

    `handler` is the object whose methods the other side may call, limited to
    the names in `allowed`. Construct it inside a running event loop, then run
    `serve()` to read what the other side sends.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        handler: object,
        *,
        allowed: frozenset[str] | set[str],
    ):
        self._reader = reader
        self._writer = writer
        self._handler = handler
        self._allowed = frozenset(allowed)
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._tasks: set[asyncio.Task] = set()
        # Everything goes out through one queue and one task, so messages keep
        # the order they were sent in and a slow reader never blocks a sender.
        # None tells the writer to finish.
        self._outgoing: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._writer_task = asyncio.create_task(self._write())
        self.closed = asyncio.Event()
        self.on_close: Callable[[], None] | None = None

    def notify(self, method: str, *args: Any, **kwargs: Any) -> None:
        """Call `method` on the other side without waiting for it; a no-op once closed."""
        if self.closed.is_set():
            return
        self._send(
            {"type": "notify", "method": method, "args": encode(args), "kwargs": encode(kwargs)}
        )

    async def request(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Call `method` on the other side and return its result."""
        if self.closed.is_set():
            raise ConnectionError("The RPC connection is closed.")
        # Encoded first, so an unsendable argument fails here with nothing pending.
        message = {"type": "request", "method": method, "args": encode(args)}
        message["kwargs"] = encode(kwargs)
        message["id"] = request_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._send(message)
        try:
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def serve(self) -> None:
        """Handle what the other side sends until it goes; the peer is closed after."""
        try:
            while (message := await read_message(self._reader)) is not None:
                await self._dispatch(message)
        finally:
            self.close()

    def close(self) -> None:
        """Flush what is queued, then close the connection. Idempotent."""
        if self.closed.is_set():
            return
        self.closed.set()
        self._outgoing.put_nowait(None)
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("The RPC connection closed."))
        self._pending.clear()
        for task in self._tasks:
            task.cancel()
        if self.on_close is not None:
            self.on_close()

    async def wait_closed(self) -> None:
        await self.closed.wait()

    def _send(self, message: dict) -> None:
        if not self.closed.is_set():
            self._outgoing.put_nowait(dumps(message))

    async def _write(self) -> None:
        try:
            while (data := await self._outgoing.get()) is not None:
                self._writer.write(data)
                await self._writer.drain()
        except OSError:
            pass
        finally:
            # Closing the transport also ends the reader, so serve() returns.
            self._writer.close()
            # A connection that cannot be written to is gone.
            self.close()

    async def _dispatch(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "notify":
            method = message.get("method")
            if method not in self._allowed:
                logger.warning("Ignored a notification for disallowed method %r.", method)
                return
            # Awaited here, not in a task, so notifications stay in order.
            try:
                await self._call(method, message.get("args", []), message.get("kwargs", {}))
            except Exception:
                logger.exception("Notification handler %r failed.", method)
        elif kind == "request":
            # A task of its own: a request can wait on the user for minutes,
            # and notifications behind it must still arrive.
            task = asyncio.create_task(self._answer(message))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        elif kind == "reply":
            future = self._pending.pop(message.get("id"), None)
            if future is None or future.done():
                return
            if (error := message.get("error")) is not None:
                future.set_exception(_remote_error(error))
            else:
                try:
                    future.set_result(decode(message.get("result")))
                except Exception as error:
                    future.set_exception(error)
        # Anything else is from a newer peer and ignored.

    async def _answer(self, message: dict) -> None:
        method = message.get("method")
        reply: dict[str, Any] = {"type": "reply", "id": message.get("id")}
        try:
            if method not in self._allowed:
                raise PermissionError(f"{method!r} may not be called remotely.")
            result = await self._call(method, message.get("args", []), message.get("kwargs", {}))
            reply["result"] = encode(result)
        except Exception as error:
            reply["error"] = {
                "type": type(error).__name__,
                "message": str(error),
                # Sent rather than inferred from the name, so ValueError
                # subclasses count too.
                "value_error": isinstance(error, ValueError),
            }
        self._send(reply)

    async def _call(self, method: str, args: list, kwargs: dict) -> Any:
        result = getattr(self._handler, method)(*decode(args), **decode(kwargs))
        if inspect.isawaitable(result):
            result = await result
        return result
