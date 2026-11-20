"""pcode's delegation, offered to Antigravity as an MCP server.

Antigravity's own agent loop may call pcode's `delegate_task`, to run a pcode
worker on another provider (`claude:`, `openai-codex:`, ...), and
`manage_worker`, to answer or message one. This is the ordinary ACP shape: the
client lists MCP servers in `session/new` and the agent decides when to call
them. A call runs through the current turn's tool manager, as the model's own
call would, so it is pcode's real delegation and counts against the turn:
it can run in the background or be detached, and its result or a worker's
question then reaches Antigravity as the turn's next prompt, as it reaches
any model. The user sees a note when a worker starts and finishes, since the
worker panel is fed only by a parent's own tool calls.

Each session's `agy` starts `antigravity_relay.py`, which joins this bridge's
Unix socket. A call made while no pcode turn is running (a background
sub-agent after the turn ended) is refused: there is no run to execute it in.

Workers never run on `antigravity:` (`WORKER_REFUSAL`): a call always names a
model, the first `subagent_models` entry when Antigravity names none, since
an omitted model means the session's own.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import contextvars
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDenied

from pcode.antigravity import WORKER_REFUSAL
from pcode.models import runs_own_loop

SERVER_NAME = "pcode"
EXPOSED = ("delegate_task", "manage_worker")
DEFAULT_AGENT = "worker"
RELAY = Path(__file__).with_name("antigravity_relay.py")
SOCKET_DIR_PREFIX = "pcode-agy-"
# A brief with long context is one JSON line; asyncio's default cap is 64 KiB.
LINE_LIMIT = 16 * 2**20
# A socket directory younger than this may be another process's, still starting.
STALE_AFTER = 60.0
NO_TURN = "pcode runs this only during one of its turns, and none is running now."
NO_MODEL = (
    "Name the model to run the sub-agent on, as provider:model; it can't run on "
    "Antigravity. The user can set a default in pcode's `subagent_models` setting (/config)."
)
INTRO = (
    "Runs a pcode sub-agent on another model provider (such as Claude or OpenAI), "
    "in this workspace with pcode's own tools, and returns its report.\n\n"
)

# Every open bridge, for `shutdown()` and the exit cleanup.
_bridges: weakref.WeakSet[ToolBridge] = weakref.WeakSet()
_swept = False


def _socket_root() -> Path:
    # Short: a Unix socket path is capped near 100 bytes, and macOS's
    # per-user temporary directory is long.
    return Path("/tmp") if Path("/tmp").is_dir() else Path(tempfile.gettempdir())


def sweep_stale() -> None:
    """Remove this user's socket directories no running pcode serves (a crash's)."""
    for directory in _socket_root().glob(f"{SOCKET_DIR_PREFIX}*"):
        try:
            info = directory.stat()
            if info.st_uid != os.getuid() or time.time() - info.st_mtime < STALE_AFTER:
                continue
        except OSError:
            continue
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        try:
            probe.connect(str(directory / "mcp.sock"))
        except OSError:
            shutil.rmtree(directory, ignore_errors=True)
        finally:
            probe.close()


async def close_all() -> None:
    await asyncio.gather(*(b.close() for b in list(_bridges)), return_exceptions=True)


@atexit.register
def _remove_sockets() -> None:
    for bridge in list(_bridges):
        if bridge._dir is not None:
            shutil.rmtree(bridge._dir, ignore_errors=True)


def _allowed(result: Any) -> Any:
    if isinstance(result, ToolDenied):
        raise RuntimeError(result.message or "Denied.")
    return result


def _text(value: Any) -> str:
    value = getattr(value, "return_value", value)  # A `ToolReturn`.
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str, indent=1)


def _error_text(error: BaseException) -> str:
    if isinstance(error, ModelRetry):
        return error.message
    return str(error) or type(error).__name__


class ToolBridge:
    """One conversation's MCP server for its `agy` sessions."""

    def __init__(self, notify: Callable[[str], None] = lambda text: None) -> None:
        # The turn calls run in, and its context variables (sandbox, deadline,
        # tracing); set while a turn streams. Tasks would otherwise copy the
        # context of whichever turn first started the server.
        self.run_context: RunContext[Any] | None = None
        self._context: contextvars.Context | None = None
        # Tells the user about the workers Antigravity runs: they reach pcode's
        # event stream only after the turn, and never the worker panel, since
        # no pcode tool call of the parent's starts them.
        self.notify = notify
        self._server: asyncio.Server | None = None
        self._dir: str | None = None
        self._calls: set[asyncio.Task] = set()
        self._listed: list[dict] = []

    async def servers(self) -> list[dict]:
        """The ACP `mcpServers` entry for a new or resumed session; none if it can't start."""
        global _swept
        if self._server is None:
            if not _swept:
                _swept = True
                await asyncio.to_thread(sweep_stale)
            directory = tempfile.mkdtemp(prefix=SOCKET_DIR_PREFIX, dir=_socket_root())
            try:
                self._server = await asyncio.start_unix_server(
                    self._serve, path=str(Path(directory) / "mcp.sock"), limit=LINE_LIMIT
                )
            except OSError:
                shutil.rmtree(directory, ignore_errors=True)
                return []
            self._dir = directory
            _bridges.add(self)
        assert self._dir is not None
        return [
            {
                "name": SERVER_NAME,
                "command": sys.executable,
                "args": ["-I", str(RELAY), str(Path(self._dir) / "mcp.sock")],
                "env": [],
            }
        ]

    def begin_turn(self, run_context: RunContext[Any] | None) -> None:
        self.run_context = run_context
        self._context = contextvars.copy_context()

    def cancel_calls(self) -> None:
        """The turn ended: cancel the calls it was running and accept no more."""
        self.run_context = self._context = None
        for task in list(self._calls):
            task.cancel()

    async def drained(self) -> None:
        """Wait for cancelled calls to finish unwinding (worker cleanup included)."""
        if self._calls:
            await asyncio.gather(*list(self._calls), return_exceptions=True)

    async def close(self) -> None:
        self.cancel_calls()
        await self.drained()
        server, self._server = self._server, None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        directory, self._dir = self._dir, None
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
        _bridges.discard(self)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # This connection's calls by request ID, and those agy gave up on.
        calls: dict[Any, asyncio.Task] = {}
        abandoned: set = set()

        def send(message: dict) -> None:
            if not writer.is_closing():
                writer.write((json.dumps(message) + "\n").encode())

        try:
            while True:
                try:
                    line = await reader.readline()
                except ValueError:  # Longer than LINE_LIMIT: the stream can't recover.
                    break
                if not line:
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                method = message.get("method")
                if method == "notifications/cancelled":
                    request_id = (message.get("params") or {}).get("requestId")
                    if (task := calls.pop(request_id, None)) is not None:
                        abandoned.add(request_id)
                        task.cancel()
                    continue
                if "id" not in message:
                    continue  # Another notification, or a response pcode never asked for.
                request_id = message["id"]
                if method == "tools/call":
                    # A copy each, so overlapping calls can't see each other's variables.
                    context = self._context.copy() if self._context is not None else None
                    task = asyncio.create_task(
                        self._call(message, send, abandoned), context=context
                    )
                    calls[request_id] = task
                    self._calls.add(task)
                    task.add_done_callback(self._calls.discard)
                    task.add_done_callback(
                        lambda done, key=request_id: calls.get(key) is done and calls.pop(key)
                    )
                    continue
                if method == "initialize":
                    params = message.get("params") or {}
                    result: dict | None = {
                        "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": SERVER_NAME, "version": "1"},
                    }
                elif method == "tools/list":
                    result = {"tools": self._tools()}
                elif method == "ping":
                    result = {}
                else:
                    result = None
                if result is None:
                    error = {"code": -32601, "message": f"Method not found: {method}"}
                    send({"jsonrpc": "2.0", "id": request_id, "error": error})
                else:
                    send({"jsonrpc": "2.0", "id": request_id, "result": result})
                await writer.drain()
        except ConnectionError, OSError:
            pass
        finally:
            # agy is gone (or gave up on the connection): nobody wants these results.
            for task in calls.values():
                task.cancel()
            writer.close()

    def _tools(self) -> list[dict]:
        """pcode's own definitions of the exposed tools, as the current turn has them."""
        ctx = self.run_context
        manager = ctx.tool_manager if ctx is not None else None
        if manager is None:
            return self._listed  # Listed outside a turn: what the last one offered.
        tools = []
        for name in EXPOSED:
            definition = manager.get_tool_def(name)
            if definition is None:
                continue
            schema = json.loads(json.dumps(definition.parameters_json_schema))
            description = definition.description or ""
            if name == "delegate_task":
                schema, description = _delegation_schema(schema), INTRO + description
            tools.append({"name": name, "description": description, "inputSchema": schema})
        self._listed = tools
        return tools

    async def _call(self, message: dict, send, abandoned: set) -> None:
        params = message.get("params") or {}
        request_id = message["id"]
        try:
            text = await self._run(str(params.get("name", "")), params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": text}]}
        except asyncio.CancelledError:
            if request_id not in abandoned:  # agy expects no reply to a call it cancelled.
                result = {
                    "content": [{"type": "text", "text": "The pcode turn ended."}],
                    "isError": True,
                }
                send({"jsonrpc": "2.0", "id": request_id, "result": result})
            abandoned.discard(request_id)
            raise
        except Exception as error:
            result = {"content": [{"type": "text", "text": _error_text(error)}], "isError": True}
        send({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def _run(self, name: str, arguments: dict) -> str:
        ctx = self.run_context
        if ctx is None or ctx.tool_manager is None:
            raise RuntimeError(NO_TURN)
        manager = ctx.tool_manager
        if name not in EXPOSED or manager.get_tool_def(name) is None:
            raise RuntimeError(f"pcode offers no tool named {name!r} here.")
        arguments = dict(arguments)
        if name == "delegate_task":
            arguments = _delegation_arguments(arguments)
        call = ToolCallPart(name, arguments, tool_call_id=f"agy_{uuid4().hex}")

        async def handle() -> Any:
            # Unwrapped: a refused call is agy's to retry, not a charge against
            # the turn's own retry budget.
            return _allowed(await manager.handle_call(call, wrap_validation_errors=False))

        if name != "delegate_task":
            return _text(await handle())
        about = arguments.get("purpose") or " ".join(str(arguments.get("task", "")).split())
        worker = f"pcode worker on {arguments['model']}"
        self.notify(f"Antigravity started a {worker}: {about[:100]}")
        try:
            text = _text(await handle())
        except BaseException:
            self.notify(f"The {worker} stopped without a report.")
            raise
        from pcode.worker_channel import hub_for

        hub = hub_for(ctx)
        still_running = hub is not None and any(
            handle.call_id == call.tool_call_id and handle.running for handle in hub.handles()
        )
        # In the background or detached, its result reaches Antigravity later.
        if not still_running:
            self.notify(f"The {worker} finished; Antigravity has its report.")
        return text


def offered_models() -> list[str]:
    """The models a sub-agent may run on: the /model picker's, less Antigravity's."""
    from pcode.models import active_providers, model_catalog

    return [name for name in model_catalog(active_providers(None)) if not runs_own_loop(name)]


def default_model() -> str | None:
    """The first `subagent_models` entry Antigravity may delegate to."""
    from pcode.preferences import subagent_models

    return next((name for name in subagent_models() if not runs_own_loop(name)), None)


def _delegation_schema(schema: dict) -> dict:
    properties = schema.setdefault("properties", {})
    # Antigravity never sees pcode's list of sub-agents, and guesses a name.
    required = [key for key in schema.get("required", []) if key != "agent_name"]
    properties["agent_name"] = {
        "type": "string",
        "default": DEFAULT_AGENT,
        "description": f"Which pcode sub-agent runs it; {DEFAULT_AGENT!r} is the general one.",
    }
    default = default_model()
    offered = ", ".join(offered_models())
    properties["model"] = {
        "type": "string",
        "description": (
            "The model to run the sub-agent on, as provider:model"
            + (f", one of: {offered}." if offered else ".")
            + (f" Defaults to {default}." if default else "")
        ),
    }
    if default is None:
        required.append("model")
    schema["required"] = list(dict.fromkeys(required))
    return schema


def _delegation_arguments(arguments: dict) -> dict:
    """`arguments` with a model the worker may run on: never Antigravity's own."""
    arguments["agent_name"] = arguments.get("agent_name") or DEFAULT_AGENT
    model = arguments.get("model") or default_model()
    if not model:
        # Omitted, the worker would run on the session's model: Antigravity.
        raise RuntimeError(NO_MODEL)
    if runs_own_loop(model):
        raise RuntimeError(f"Cannot use {model}: {WORKER_REFUSAL}")
    arguments["model"] = model
    return arguments
