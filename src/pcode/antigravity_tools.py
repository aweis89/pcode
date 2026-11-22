"""pcode's tools, offered to Antigravity as an MCP server.

Antigravity runs its own agent loop with its own files, shell, search and
planning tools, so pcode does not duplicate those. Everything that belongs to
pcode itself — delegation to workers on other providers (`delegate_task`,
`manage_worker`, `task_worktree`), `/advisor`, spilled tool-output retrieval
(`read_tool_result`, `grep_tool_result`), and pcode extensions that Antigravity
doesn't replace (`/browser`, `session_history`, `conductor`, or any extension
with `bridge_to_acp = True`) — is offered the ordinary ACP way, as a stdio MCP
server in `session/new` and `session/resume`.

A call runs through the current turn's tool manager, as any model's call would,
so capability hooks (`Browser.before_tool_execute`, output limits, workspace
guards) and delegation accounting work identically. Multimodal `ToolReturn`
images (such as browser screenshots) are forwarded as MCP image blocks, and
bridged capabilities' instructions are returned from `initialize` so `agy`
exposes them as the server's `instructions.md`.

Each session's `agy` starts `antigravity_relay.py`, which joins this bridge's
Unix socket. A call made while no pcode turn is running (a background
sub-agent after the turn ended) is refused: there is no run to execute it in.

When Antigravity omits `model` in `delegate_task`, the worker defaults to the
session's own current Antigravity model (running in an isolated `agy` session),
matching how every other provider in pcode delegates. Bridged `delegate_task`
calls emit `BridgedToolStarted`/`BridgedToolFinished` into the run's event
stream so workers appear live in the tool panel and `/agents`.
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import contextlib
import contextvars
import inspect
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import anyio
from pydantic_ai import CustomEvent, ModelRetry, RunContext
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import (
    BinaryContent,
    TextContent,
    ToolCallPart,
    ToolReturn,
    ToolReturnPart,
)
from pydantic_ai.tools import ToolDenied

from pcode.antigravity import PREFIX, unwrap

SERVER_NAME = "pcode"
# Core pcode tools exposed whenever the turn's tool manager offers them.
CORE_TOOLS = (
    "delegate_task",
    "manage_worker",
    "task_worktree",
    "advisor",
    "read_tool_result",
    "grep_tool_result",
)
# Capability IDs whose tools and instructions are bridged to Antigravity when active.
# Any capability with `bridge_to_acp = True` is bridged the same way.
BRIDGED_CAPABILITIES = frozenset(
    {
        "browser",
        "ext.browser",
        "ext.session_history",
        "ext.conductor",
    }
)
DEFAULT_AGENT = "worker"
RELAY = Path(__file__).with_name("antigravity_relay.py")
SOCKET_DIR_PREFIX = "pcode-agy-"
# A brief with long context is one JSON line; asyncio's default cap is 64 KiB.
LINE_LIMIT = 16 * 2**20
# A socket directory younger than this may be another process's, still starting.
STALE_AFTER = 60.0
NO_TURN = "pcode runs this only during one of its turns, and none is running now."
# Seconds a bridged MCP call waits for a pending post-settle continuation turn
# to enter `begin_turn()` before rejecting with NO_TURN.
WAKE_TURN_WAIT = 2.0
INTRO = (
    "Preferred over `invoke_subagent`: runs a pcode sub-agent in this workspace (or an "
    "isolated worktree), streaming its progress live in the user's task panel and `/agents`. "
    "Omit `model` to use the session's current model, or pass another provider:model (such as "
    "Claude or OpenAI). Set `purpose` and pass `run_in_background=True` for parallel or "
    "long-running tasks (over ~2 minutes).\n\n"
)
DELEGATION_PROMPT_NOTE = (
    "Always use `mcp_pcode_delegate_task` (with `mcp_pcode_manage_worker` and "
    "`mcp_pcode_task_worktree`) instead of `invoke_subagent` for sub-agent delegation "
    "(omit `model` to use the session's current model, or pass another `provider:model` "
    "or `workspace_mode='isolated'`). Unlike `invoke_subagent`, pcode shows "
    "`mcp_pcode_delegate_task` sub-agents live in the user's task panel and `/agents` "
    "and keeps the turn active while they run; set `purpose` and pass "
    "`run_in_background=True` for parallel or long-running tasks (over ~2 minutes). "
    "Never stop a running pcode worker to redo its task with `invoke_subagent`: a quiet "
    "worker or one showing 0 tokens is normally mid model call, so check `liveness` in "
    "`mcp_pcode_manage_worker` list and keep waiting."
)


@dataclass(kw_only=True)
class BridgedToolStarted(CustomEvent):
    """A pcode tool (`delegate_task`) started via the ACP MCP bridge."""

    call: ToolCallPart


@dataclass(kw_only=True)
class BridgedToolFinished(CustomEvent):
    """A bridged pcode tool call finished (or failed / was cancelled)."""

    result: ToolReturnPart


# Added eagerly to `navigate` so Antigravity sees the sign-in safety rule even
# if it never reads this MCP server's `instructions.md`.
BROWSER_NAVIGATE_NOTE = (
    "\n\nNever type credentials on a sign-in page: call `browser_open` so the window is "
    "in front and ask the user to log in there. Strip the leading robot-face marker from "
    "tab titles you report. Read this MCP server's instructions.md for full browser guidance."
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


def _item_blocks(item: Any) -> list[dict]:
    if isinstance(item, TextContent):
        return [{"type": "text", "text": item.content}] if item.content else []
    if isinstance(item, str):
        return [{"type": "text", "text": item}] if item else []
    if isinstance(item, BinaryContent):
        if item.is_image:
            encoded = base64.b64encode(item.data).decode()
            return [{"type": "image", "data": encoded, "mimeType": item.media_type}]
        return [
            {
                "type": "text",
                "text": f"[binary content: {item.media_type}, {len(item.data):,} bytes]",
            }
        ]
    return [{"type": "text", "text": json.dumps(item, default=str, indent=1)}]


def _value_blocks(value: Any, *, omit_none: bool = False) -> list[dict]:
    if value is None and omit_none:
        return []
    if isinstance(value, (BinaryContent, TextContent)):
        return _item_blocks(value)
    if isinstance(value, str):
        return [{"type": "text", "text": value}] if value else []
    if isinstance(value, (list, tuple)) and any(
        isinstance(item, (BinaryContent, TextContent)) for item in value
    ):
        blocks: list[dict] = []
        for item in value:
            blocks.extend(_item_blocks(item))
        return blocks
    return [{"type": "text", "text": json.dumps(value, default=str, indent=1)}]


def _content(value: Any) -> list[dict]:
    """Convert a Pydantic AI tool result (including `ToolReturn` images) to MCP content blocks."""
    blocks: list[dict] = []
    if isinstance(value, ToolReturn):
        blocks.extend(_value_blocks(value.return_value, omit_none=value.content is not None))
        extra = value.content
        if extra is not None:
            items = list(extra) if isinstance(extra, (list, tuple)) else [extra]
            for item in items:
                blocks.extend(_item_blocks(item))
    else:
        blocks.extend(_value_blocks(value))
    return blocks or [{"type": "text", "text": ""}]


def _error_text(error: BaseException) -> str:
    if isinstance(error, ModelRetry):
        return error.message
    return str(error) or type(error).__name__


def _toolset_names(toolset: Any) -> list[str]:
    """Tool names owned by `toolset`, walking combined, prefixed, renamed, and wrapped toolsets."""
    if toolset is None:
        return []
    if isinstance(tools := getattr(toolset, "tools", None), dict):
        return list(tools.keys())
    if (parts := getattr(toolset, "toolsets", None)) is not None:
        names: list[str] = []
        for part in parts:
            names.extend(_toolset_names(part))
        return names
    if (wrapped := getattr(toolset, "wrapped", None)) is not None:
        names = _toolset_names(wrapped)
        if isinstance(prefix := getattr(toolset, "prefix", None), str) and prefix:
            return [f"{prefix}_{name}" for name in names]
        if isinstance(name_map := getattr(toolset, "name_map", None), dict):
            # RenamedToolset maps new_name -> original_name.
            original_to_new = {original: new for new, original in name_map.items()}
            return [original_to_new.get(name, name) for name in names]
        return names
    return []


def _capability_tool_names(capability: Any, *, depth: int = 0) -> list[str]:
    """Tool names owned by `capability`, unwrapping dynamic, combined, and bridge wrappers."""
    if capability is None or depth > 8:
        return []
    if (children := getattr(capability, "capabilities", None)) is not None:
        names: list[str] = []
        for child in children:
            names.extend(_capability_tool_names(child, depth=depth + 1))
        return names
    wrapped = getattr(capability, "wrapped", None)
    if wrapped is not None and (
        getattr(capability, "bridge_to_acp", False)
        or type(capability).__name__ == "ResolvedDynamicCapability"
    ):
        return _capability_tool_names(wrapped, depth=depth + 1)
    getter = getattr(capability, "get_toolset", None)
    names = _toolset_names(getter()) if callable(getter) else []
    if names:
        return names
    if wrapped is not None:
        return _capability_tool_names(wrapped, depth=depth + 1)
    return []


def _is_bridged_capability(cap_id: str, capability: Any) -> bool:
    if cap_id in BRIDGED_CAPABILITIES:
        return True
    current = capability
    for _ in range(8):
        if getattr(current, "bridge_to_acp", False):
            return True
        current = getattr(current, "wrapped", None)
        if current is None:
            return False
    return False


def _extract_instruction_texts(raw: Any, ctx: RunContext[Any], *, depth: int = 0) -> list[str]:
    if raw is None or depth > 8:
        return []
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, (list, tuple)):
        texts: list[str] = []
        for item in raw:
            texts.extend(_extract_instruction_texts(item, ctx, depth=depth + 1))
        return texts
    if callable(raw):
        try:
            params = inspect.signature(raw).parameters
        except ValueError, TypeError:
            params = None
        try:
            result = raw() if params is not None and len(params) == 0 else raw(ctx)
        except Exception:
            return []
        if inspect.iscoroutine(result):
            result.close()
            return []
        return _extract_instruction_texts(result, ctx, depth=depth + 1)
    content = getattr(raw, "content", None)
    if isinstance(content, str) and content.strip():
        return [content.strip()]
    return []


def _capability_instructions(capability: Any, ctx: RunContext[Any]) -> list[str]:
    getter = getattr(capability, "get_instructions", None)
    if not callable(getter):
        return []
    return _extract_instruction_texts(getter(), ctx)


def _exposed_names(ctx: RunContext[Any]) -> list[str]:
    """Tool names bridged to Antigravity for `ctx`, in stable order."""
    manager = ctx.tool_manager
    if manager is None:
        return []
    candidates: list[str] = list(CORE_TOOLS)
    for cap_id, capability in (ctx.capabilities or {}).items():
        if not _is_bridged_capability(cap_id, capability):
            continue
        candidates.extend(_capability_tool_names(capability))
    return [name for name in dict.fromkeys(candidates) if manager.get_tool_def(name) is not None]


def _bridge_instructions(ctx: RunContext[Any], exposed: list[str]) -> str:
    """Combined instructions for the capabilities currently bridged to Antigravity."""
    capabilities = ctx.capabilities or {}
    parts: list[str] = []
    for cap_id, capability in capabilities.items():
        if _is_bridged_capability(cap_id, capability):
            parts.extend(_capability_instructions(capability, ctx))
        elif cap_id == "tool_output_limits" and "read_tool_result" in exposed:
            parts.extend(_capability_instructions(capability, ctx))
    return "\n\n".join(dict.fromkeys(parts))


def repo_context_instructions(ctx: RunContext[Any] | None) -> str | None:
    """Repository instruction files pcode discovered that `agy` does not load natively."""
    if ctx is None:
        return None
    from pcode.context_breakdown import REPO_CONTEXT

    capability = (ctx.capabilities or {}).get(REPO_CONTEXT)
    render = getattr(capability, "instructions_for_antigravity", None)
    return render() if callable(render) else None


def _system_prompt_append(ctx: RunContext[Any] | None) -> str:
    """Concise pcode-specific instructions appended to `agy`'s system prompt on `session/new`."""
    from pcode.agent import PCODE_CLIENT_INSTRUCTIONS

    parts: list[str] = [PCODE_CLIENT_INSTRUCTIONS]
    if ctx is not None:
        if repo := repo_context_instructions(ctx):
            parts.append(repo)
        if ctx.tool_manager is not None:
            exposed = _exposed_names(ctx)
            if "delegate_task" in exposed:
                parts.append(DELEGATION_PROMPT_NOTE)
            if "advisor" in exposed:
                for cap_id, capability in (ctx.capabilities or {}).items():
                    if cap_id in ("advisor", "advisor_guidance"):
                        parts.extend(_capability_instructions(capability, ctx))
    return "\n\n".join(dict.fromkeys(parts))


class ToolBridge:
    """One conversation's MCP server for its `agy` sessions."""

    def __init__(
        self,
        notify: Callable[[str], None] = lambda text: None,
        *,
        wake_starting: Callable[[], bool] = lambda: False,
    ) -> None:
        # The turn calls run in, and its context variables (sandbox, deadline,
        # tracing); set while a turn streams. Tasks would otherwise copy the
        # context of whichever turn first started the server.
        self.run_context: RunContext[Any] | None = None
        self._context: contextvars.Context | None = None
        self._wake_starting = wake_starting
        self._turn_ready = asyncio.Event()
        # Retained for callers passing `notify=`; bridged `delegate_task` calls
        # now stream directly into the live panel and `/agents` via `ctx.emit`.
        self.notify = notify
        self._server: asyncio.Server | None = None
        self._dir: str | None = None
        self._calls: set[asyncio.Task] = set()
        self._listed: list[dict] = []
        self._instructions_text: str = ""

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
        if run_context is not None:
            self._turn_ready.set()
        else:
            self._turn_ready.clear()

    def abort_wake(self) -> None:
        """Wake any MCP call waiting on a cancelled/aborted continuation turn."""
        self._turn_ready.set()
        if self.run_context is None:
            self._turn_ready.clear()

    def signature(self) -> tuple[tuple[tuple[str, str, str], ...], str] | None:
        """Fingerprint of the bridged tool schemas and instructions for the current turn."""
        ctx = self.run_context
        if ctx is None or ctx.tool_manager is None:
            return None
        tools = self._build_tools(ctx)
        tool_sig = tuple(
            (t["name"], t["description"], json.dumps(t["inputSchema"], sort_keys=True))
            for t in tools
        )
        return tool_sig, _bridge_instructions(ctx, [t["name"] for t in tools])

    def cancel_calls(self) -> None:
        """The turn ended: cancel the calls it was running and accept no more."""
        self.run_context = self._context = None
        self._turn_ready.clear()
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
                    if instructions := self._instructions():
                        result["instructions"] = instructions
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

    def _instructions(self) -> str:
        ctx = self.run_context
        if ctx is None or ctx.tool_manager is None:
            return self._instructions_text
        text = _bridge_instructions(ctx, _exposed_names(ctx))
        self._instructions_text = text
        return text

    def system_prompt_append(self) -> str:
        """pcode-specific system prompt addition for `session/new` `_meta`."""
        return _system_prompt_append(self.run_context)

    def _build_tools(self, ctx: RunContext[Any]) -> list[dict]:
        manager = ctx.tool_manager
        if manager is None:
            return []
        has_browser = "browser" in (ctx.capabilities or {})
        tools = []
        for name in _exposed_names(ctx):
            definition = manager.get_tool_def(name)
            if definition is None:
                continue
            schema = json.loads(json.dumps(definition.parameters_json_schema))
            description = definition.description or ""
            if name == "delegate_task":
                schema, description = _delegation_schema(schema, ctx), INTRO + description
            elif name == "navigate" and has_browser:
                description += BROWSER_NAVIGATE_NOTE
            tools.append({"name": name, "description": description, "inputSchema": schema})
        return tools

    def _tools(self) -> list[dict]:
        """pcode's own definitions of the exposed tools, as the current turn has them."""
        ctx = self.run_context
        if ctx is None or ctx.tool_manager is None:
            return self._listed  # Listed outside a turn: what the last one offered.
        tools = self._build_tools(ctx)
        self._listed = tools
        return tools

    async def _call(self, message: dict, send, abandoned: set) -> None:
        params = message.get("params") or {}
        request_id = message["id"]
        try:
            blocks = await self._run(str(params.get("name", "")), params.get("arguments") or {})
            result = {"content": blocks}
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

    async def _run(self, name: str, arguments: dict) -> list[dict]:
        ctx = self.run_context
        if (ctx is None or ctx.tool_manager is None) and self._wake_starting():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._turn_ready.wait(), WAKE_TURN_WAIT)
            ctx = self.run_context
        if ctx is None or ctx.tool_manager is None:
            raise RuntimeError(NO_TURN)
        manager = ctx.tool_manager
        if name not in _exposed_names(ctx) or manager.get_tool_def(name) is None:
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
            return _content(await handle())
        await _emit_bridged(ctx, BridgedToolStarted(call=call))
        try:
            raw = await handle()
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                outcome = "interrupted"
                message = "Cancelled"
            elif isinstance(error, ModelRetry):
                outcome = "retried"
                message = _error_text(error)
            else:
                outcome = "failed"
                message = _error_text(error)
            failed = ToolReturnPart(
                tool_name=name,
                content=message,
                tool_call_id=call.tool_call_id,
                outcome=outcome,
            )
            with anyio.CancelScope(shield=True):
                await _emit_bridged(ctx, BridgedToolFinished(result=failed))
            raise
        content_value = raw.return_value if isinstance(raw, ToolReturn) else raw
        await _emit_bridged(
            ctx,
            BridgedToolFinished(
                result=ToolReturnPart(name, content_value, tool_call_id=call.tool_call_id)
            ),
        )
        return _content(raw)


async def _emit_bridged(ctx: RunContext[Any], event: CustomEvent) -> None:
    if getattr(ctx, "_event_stream_buffer", None) is None:
        return
    with contextlib.suppress(UserError):
        await ctx.emit(event)


def offered_models() -> list[str]:
    """The models a sub-agent may run on: everything in the /model picker."""
    from pcode.models import active_providers, model_catalog

    return list(model_catalog(active_providers(None)))


def _session_model(ctx: RunContext[Any] | None) -> str:
    """The session's current `antigravity:<model>` name, or "" when unknown."""
    if ctx is None:
        return ""
    native = unwrap(getattr(ctx, "model", None))
    return f"{PREFIX}{native.model_name}" if native is not None and native.model_name else ""


def _subagent_names(ctx: RunContext[Any] | None) -> list[str]:
    if ctx is None:
        return []
    sub = (ctx.capabilities or {}).get("sub_agents")
    return [
        name
        for agent in getattr(sub, "agents", ())
        if (name := getattr(agent, "resolved_name", None))
    ]


def _delegation_schema(schema: dict, ctx: RunContext[Any] | None = None) -> dict:
    properties = schema.setdefault("properties", {})
    # Antigravity never sees pcode's list of sub-agents, and guesses a name.
    required = [key for key in schema.get("required", []) if key not in {"agent_name", "model"}]
    agents = _subagent_names(ctx)
    available = f", one of: {', '.join(agents)}" if len(agents) > 1 else ""
    properties["agent_name"] = {
        "type": "string",
        "default": DEFAULT_AGENT,
        "description": (
            f"Which pcode sub-agent runs it{available}; {DEFAULT_AGENT!r} is the general one."
        ),
    }
    current = _session_model(ctx)
    default_note = (
        f" Omit it to use the session's current model ({current})."
        if current
        else " Omit it to use the session's current model."
    )
    offered = ", ".join(offered_models())
    properties["model"] = {
        "type": "string",
        "description": (
            "Optional model to run the sub-agent on, as provider:model"
            + (f", one of: {offered}." if offered else ".")
            + default_note
        ),
    }
    schema["required"] = list(dict.fromkeys(required))
    return schema


def _delegation_arguments(arguments: dict) -> dict:
    """Fill in the default agent name; an omitted model uses the session's own."""
    if not str(arguments.get("agent_name") or "").strip():
        arguments["agent_name"] = DEFAULT_AGENT
    if not str(arguments.get("model") or "").strip():
        arguments.pop("model", None)
    return arguments
