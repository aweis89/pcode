"""`claude:` models: Claude Code's own login through the Claude Agent SDK, no proxy.

Each conversation keeps one `claude` CLI process (the one bundled with
`claude-agent-sdk`) alive across requests, where Meridian starts a fresh one per
request behind a Node proxy. pcode's tools are served to that process as an
in-process MCP server whose handlers *park*: the model calls a tool, the CLI
invokes the handler, the streamed assistant message ends, and Pydantic AI runs
the tool itself. The next request carries the result, which releases the parked
handler, and the CLI goes on to its next API call. The CLI streams raw Anthropic
events, so parsing reuses `AnthropicModel`'s streamed-response code.

pcode's history stays the source of truth; the CLI transcript is a disposable
copy. A request continues a live process only when that process holds exactly
the history before the new user message. Otherwise it forks the CLI transcript
at the last assistant message both share (`resume` + `fork_session` +
`resume_session_at`: structured history, warm cache), found through a persisted
index of history hashes, and failing that starts over with the history replayed
as text, as Meridian does. See docs/anthropic-providers.md.

pcode holds no credentials here: the CLI signs in itself (`/login claude`).
"""

import asyncio
import hashlib
import json
import logging
import os
import sys
import time
import weakref
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, replace
from functools import cached_property
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic
from anthropic._models import construct_type
from anthropic.types.beta import BetaRawMessageStreamEvent
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.models import (
    ModelRequestContext,
    ModelRequestParameters,
    StreamedResponse,
    check_allow_model_requests,
)
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

logger = logging.getLogger(__name__)

PREFIX = "claude:"
SERVER = "pcode"
# How the CLI names an MCP tool to the model; pcode's own names are restored.
TOOL_PREFIX = f"mcp__{SERVER}__"
# The CLI sends the model's tool_use id with each MCP call under this key.
TOOL_USE_ID = "claudecode/toolUseId"
# Model setting carrying the workspace the CLI runs in (see `ClaudeWorkspace`).
CWD_SETTING = "pcode_claude_cwd"
EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
# Idle processes kept for reuse; busy ones are never counted or closed. Each
# holds about 300 MB, and forking a transcript is warm anyway, so keeping one
# only saves the ~0.8 s start: keep few, and not for long.
MAX_IDLE_SESSIONS = 2
IDLE_SECONDS = 10 * 60
CLOSE_TIMEOUT_SECONDS = 5.0
# A finished turn's result follows its last message within milliseconds.
TURN_END_TIMEOUT_SECONDS = 60.0
INDEX_LIMIT = 4000
REPLAY_INTRO = (
    "This conversation began outside the current session, so its earlier messages are "
    "replayed below as a transcript. Treat them as having happened here and continue "
    "from the final message.\n\n"
)

# The child inherits pcode's environment. An empty value is unset to the CLI
# (verified: requests then bill the subscription), so pcode's own Anthropic
# key, token or endpoint can never silently redirect or bill it.
CLI_ENV = {
    "ANTHROPIC_API_KEY": "",
    "ANTHROPIC_AUTH_TOKEN": "",
    "ANTHROPIC_BASE_URL": "",
    # pcode owns retries (visible in the UI), compaction and tool deferral.
    "CLAUDE_CODE_MAX_RETRIES": "0",
    "DISABLE_AUTO_COMPACT": "1",
    "ENABLE_TOOL_SEARCH": "false",
    # pcode already bounds tool output; never let the CLI truncate it again.
    "MAX_MCP_OUTPUT_TOKENS": "1000000",
    # A parked handler lasts as long as the tool runs, delegations included.
    "MCP_TOOL_TIMEOUT": str(7 * 24 * 3600 * 1000),
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_AUTOUPDATER": "1",
}
# Statuses for the CLI's error kinds when it reports no HTTP status itself.
ERROR_STATUS = {
    "authentication_failed": 401,
    "rate_limit": 429,
    "invalid_request": 400,
    "server_error": 500,
}


class ClaudeHTTPError(ModelHTTPError):
    """An API error the CLI reported for a `claude:` request."""


class ClaudeProcessError(ModelAPIError):
    """The CLI process ended mid-request; a retry starts another (transient)."""


class ClaudeStartError(ModelAPIError):
    """The CLI process could not be started or resumed."""


def cli_path() -> str | None:
    """The CLI the SDK runs: its bundled binary, else `claude` on PATH."""
    import shutil

    try:
        import claude_agent_sdk
    except ImportError:
        return shutil.which("claude")
    name = "claude.exe" if sys.platform == "win32" else "claude"
    bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / name
    return str(bundled) if bundled.is_file() else shutil.which("claude")


def failure_hint(error: BaseException) -> str | None:
    """What to do about a failed `claude:` request, or None for any other provider."""
    seen = set()
    while error is not None and id(error) not in seen and len(seen) < 16:
        seen.add(id(error))
        if isinstance(error, ClaudeHTTPError):
            body = error.body if isinstance(error.body, dict) else {}
            kind = (body.get("error") or {}).get("type")
            if error.status_code in (401, 403) or kind == "authentication_failed":
                return "Claude Code is not signed in, or its login expired. Run /login claude."
            return None
        if isinstance(error, ClaudeStartError):
            return f"{error.message} Check the Claude Code install, or run /login claude."
        if isinstance(error, ClaudeProcessError):
            return f"{error.message} Retry; pcode starts a new Claude Code process."
        error = error.__cause__ or error.__context__
    return None


# --- History bookkeeping -------------------------------------------------------


def _clean(value: Any) -> Any:
    """Drop cache markers: the CLI places its own, and they never change content."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _blocks(message: dict) -> list[dict]:
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    return list(content or [])


def normalize(messages: list[dict]) -> list[dict]:
    """Anthropic messages without cache markers, consecutive same-role turns merged.

    Pydantic AI leaves an appended reminder as its own user message; the API
    merges consecutive turns anyway, and one user turn is one CLI input.
    """
    merged: list[dict] = []
    for message in _clean(messages):
        if merged and merged[-1]["role"] == message["role"]:
            merged[-1] = {
                "role": message["role"],
                "content": _blocks(merged[-1]) + _blocks(message),
            }
        else:
            merged.append({"role": message["role"], "content": _blocks(message)})
    return merged


def _digest(previous: str, message: dict) -> str:
    text = json.dumps(message, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{previous}\n{text}".encode()).hexdigest()


def lineage(messages: list[dict]) -> list[str]:
    """One hash per message, each covering the whole history up to it."""
    chain, previous = [], "pcode-claude-1"
    for message in messages:
        previous = _digest(previous, message)
        chain.append(previous)
    return chain


def _tool_use_ids(message: dict) -> tuple[str, ...]:
    return tuple(b["id"] for b in _blocks(message) if b.get("type") == "tool_use")


def _result_ids(message: dict) -> set[str]:
    return {b["tool_use_id"] for b in _blocks(message) if b.get("type") == "tool_result"}


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    return "\n".join(_render_block(item) for item in content or [])


def _render_block(block: dict) -> str:
    kind = block.get("type")
    if kind == "text":
        return block.get("text", "")
    if kind == "tool_use":
        arguments = json.dumps(block.get("input"), ensure_ascii=False, default=str)
        return f"[Called tool {block.get('name')} (id {block.get('id')}) with {arguments}]"
    if kind == "tool_result":
        status = " (error)" if block.get("is_error") else ""
        return f"[Result of tool call {block.get('tool_use_id')}{status}]\n{_result_text(block)}"
    if kind in ("thinking", "redacted_thinking"):
        return ""
    return f"[{kind or 'content'} omitted]"


def replay(messages: list[dict], open_tool_ids: tuple[str, ...] = ()) -> list[dict]:
    """One user turn carrying `messages`, for a CLI transcript that lacks them.

    Results for the tool calls the transcript ends on (`open_tool_ids`) stay
    structured, since the API requires them; everything else becomes text.
    Images and documents from the final message are still attached.
    """
    first = _blocks(messages[0]) if messages else []
    head = [
        b for b in first if b.get("type") == "tool_result" and b["tool_use_id"] in open_tool_ids
    ]
    answered = {b["tool_use_id"] for b in head}
    head += [
        {
            "type": "tool_result",
            "tool_use_id": i,
            "content": "[No result recorded]",
            "is_error": True,
        }
        for i in open_tool_ids
        if i not in answered
    ]
    rest = [
        {"role": messages[0]["role"], "content": [b for b in first if b not in head]},
        *messages[1:],
    ]
    turns = []
    for message in rest:
        text = "\n".join(filter(None, (_render_block(b) for b in _blocks(message))))
        if text:
            turns.append(f"[{message['role'].title()}]\n{text}")
    attached = [b for b in _blocks(messages[-1]) if b.get("type") in ("image", "document")]
    body = "\n\n".join(turns)
    text = f"{REPLAY_INTRO}<conversation>\n{body}\n</conversation>" if turns else ""
    return head + ([{"type": "text", "text": text}] if text else []) + attached


def _mcp_content(block: dict) -> list[dict]:
    """An Anthropic tool_result's content as MCP content blocks."""
    content = block.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    items = []
    for item in content or []:
        source = item.get("source") or {}
        if item.get("type") == "text":
            items.append({"type": "text", "text": item.get("text", "")})
        elif item.get("type") == "image" and source.get("type") == "base64":
            items.append(
                {"type": "image", "data": source["data"], "mimeType": source["media_type"]}
            )
        else:
            items.append({"type": "text", "text": _render_block(item)})
    return items


@dataclass(frozen=True)
class ForkPoint:
    """Where one assistant message pcode received lives in a CLI transcript."""

    session_id: str
    uuid: str
    cwd: str


def index_path() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return state / "pcode" / "claude-sessions.jsonl"


class ResumeIndex:
    """History hash -> fork point, persisted so a restarted pcode resumes warm.

    Append-only lines shared by every pcode process; losing an entry only
    costs a replay, so concurrent writers need no lock beyond `O_APPEND`.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._entries: dict[str, ForkPoint] | None = None

    def _file(self) -> Path:
        return self.path or index_path()

    def _load(self) -> dict[str, ForkPoint]:
        entries: dict[str, ForkPoint] = {}
        try:
            lines = self._file().read_text().splitlines()
        except OSError:
            return entries
        for line in lines:
            try:
                row = json.loads(line)
                entries[row["key"]] = ForkPoint(row["session"], row["uuid"], row["cwd"])
            except (ValueError, KeyError, TypeError):
                continue
        if len(lines) > INDEX_LIMIT:
            kept = list(entries.items())[-INDEX_LIMIT // 2 :]
            entries = dict(kept)
            with suppress(OSError):
                temporary = self._file().with_suffix(".tmp")
                temporary.write_text("".join(self._line(k, p) for k, p in kept))
                temporary.replace(self._file())
        return entries

    @staticmethod
    def _line(key: str, point: ForkPoint) -> str:
        row = {"key": key, "session": point.session_id, "uuid": point.uuid, "cwd": point.cwd}
        return json.dumps(row) + "\n"

    async def load(self) -> None:
        if self._entries is None:
            self._entries = await asyncio.to_thread(self._load)

    def get(self, key: str) -> ForkPoint | None:
        return (self._entries or {}).get(key)

    def add(self, key: str, point: ForkPoint) -> None:
        if self._entries is None:
            self._entries = {}
        if self._entries.get(key) == point:
            return
        self._entries[key] = point
        try:
            self._file().parent.mkdir(parents=True, exist_ok=True)
            with self._file().open("a") as file:
                file.write(self._line(key, point))
        except OSError:
            logger.debug("could not record a Claude fork point", exc_info=True)


# --- One CLI process -----------------------------------------------------------


@dataclass(frozen=True)
class SessionConfig:
    """Everything fixed for the life of one CLI process."""

    model: str
    cwd: str
    system_prompt: str
    tools: str  # canonical JSON of [{name, description, input_schema}]
    effort: str | None = None
    thinking: str | None = None  # canonical JSON of the thinking setting

    def options(self, server, resume: ForkPoint | None, stderr) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        thinking = json.loads(self.thinking) if self.thinking else None
        if thinking is not None and thinking.get("type") != "disabled":
            thinking = {"display": "summarized", **thinking}
        return ClaudeAgentOptions(
            # No built-in tools, settings, CLAUDE.md, plugins or other MCP
            # servers: the model sees pcode's prompt and pcode's tools only.
            tools=[],
            setting_sources=[],
            strict_mcp_config=True,
            mcp_servers={SERVER: {"type": "sdk", "name": SERVER, "instance": server}},
            allowed_tools=[f"mcp__{SERVER}"],
            system_prompt=self.system_prompt,
            include_partial_messages=True,
            model=self.model,
            cwd=self.cwd,
            env=dict(CLI_ENV),
            effort=self.effort,
            thinking=thinking,
            # Readable thinking for scrollback when no thinking setting says so.
            extra_args={} if thinking is not None else {"thinking-display": "summarized"},
            resume=resume.session_id if resume else None,
            fork_session=resume is not None,
            resume_session_at=resume.uuid if resume else None,
            stderr=stderr,
        )


def _client_factory(options) -> Any:
    from claude_agent_sdk import ClaudeSDKClient

    return ClaudeSDKClient(options)


class ClaudeSession:
    """One live CLI process and the pcode history its transcript holds."""

    def __init__(self, config: SessionConfig, resume: ForkPoint | None = None) -> None:
        self.config = config
        self.resume = resume
        # History hashes the transcript holds, always ending on an assistant message.
        self.chain: list[str] = []
        # Tool calls the last response made, whose results the CLI is waiting for.
        self.open_tool_ids: tuple[str, ...] = ()
        self.cli_session_id: str | None = None
        self.response_uuid: str | None = None
        # Whether the current response was read through its message_stop.
        self.complete = False
        self.busy = False
        self.dead = False
        self.last_used = time.monotonic()
        self._client: Any = None
        self._pump_task: asyncio.Task | None = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._slots: dict[str, asyncio.Future] = {}
        self._delivered: set[str] = set()
        self._last_uuid: str | None = None
        self._final = False
        self._turn_done = asyncio.Event()
        self._turn_done.set()
        self._api_error: tuple[str | None, str] | None = None
        self._stderr: deque[str] = deque(maxlen=20)
        self._closed = False

    # Lifecycle

    async def connect(self, factory=None) -> None:
        options = self.config.options(self._server(), self.resume, self._stderr.append)
        self._client = (factory or _client_factory)(options)
        try:
            await self._client.connect()
        except Exception as error:
            self.dead = True
            detail = "; ".join(self._stderr) or str(error) or type(error).__name__
            raise ClaudeStartError(
                self.config.model, f"Claude Code could not start: {detail[:500]}"
            ) from error
        self._pump_task = asyncio.create_task(self._pump(), name="claude-sdk-pump")

    async def close(self) -> None:
        """Stop the process without letting it act on its own first.

        A process closed while parked records the missing results as errors and
        carries on (verified: two more billed requests), so interrupt the turn.
        """
        if self._closed:
            return
        self._closed = self.dead = True
        client = self._client
        if client is not None:
            if not self._turn_done.is_set():
                with suppress(Exception):
                    async with asyncio.timeout(CLOSE_TIMEOUT_SECONDS):
                        await client.interrupt()
            with suppress(Exception):
                await client.disconnect()
        if self._pump_task is not None:
            self._pump_task.cancel()
        for future in self._slots.values():
            if not future.done():
                future.cancel()

    # Tool handlers

    def _server(self) -> Any:
        # Built on mcp 2's constructor callbacks: the SDK helper would hide the
        # request `_meta` that carries the tool_use id.
        import mcp.types as types
        from mcp.server import Server

        tools = [
            types.Tool.model_validate(
                {
                    "name": tool["name"],
                    "description": tool.get("description") or "",
                    "inputSchema": tool["input_schema"],
                    # Keep results inline: pcode already spills large ones.
                    "_meta": {"anthropic/maxResultSizeChars": 10_000_000},
                }
            )
            for tool in json.loads(self.config.tools)
        ]

        async def list_tools(ctx, params):
            return types.ListToolsResult(tools=tools)

        async def call_tool(ctx, params):
            meta = params.meta
            extra = meta if isinstance(meta, dict) else (getattr(meta, "model_extra", None) or {})
            tool_use_id = extra.get(TOOL_USE_ID)
            if not isinstance(tool_use_id, str):
                return types.CallToolResult(
                    content=[
                        types.TextContent(type="text", text="pcode could not match this call.")
                    ],
                    isError=True,
                )
            # Parked until pcode runs the tool and sends its result.
            block = await self._slot(tool_use_id)
            return types.CallToolResult.model_validate(
                {"content": _mcp_content(block), "isError": bool(block.get("is_error"))}
            )

        return Server(SERVER, on_list_tools=list_tools, on_call_tool=call_tool)

    def _slot(self, tool_use_id: str) -> asyncio.Future:
        future = self._slots.get(tool_use_id)
        if future is None:
            future = self._slots[tool_use_id] = asyncio.get_running_loop().create_future()
        return future

    # Input

    async def _write(self, content: list[dict]) -> None:
        async def message():
            yield {
                "type": "user",
                "message": {"role": "user", "content": content},
                "parent_tool_use_id": None,
            }

        await self._client.query(message())

    async def start(self, content: list[dict]) -> None:
        """Begin a turn with a new user message."""
        try:
            async with asyncio.timeout(TURN_END_TIMEOUT_SECONDS):
                await self._turn_done.wait()
        except TimeoutError:
            self.dead = True
            raise ClaudeProcessError(
                self.config.model, "Claude Code did not finish its previous turn."
            ) from None
        self._turn_done.clear()
        self.complete = False
        await self._write(content)

    async def answer(self, message: dict) -> None:
        """Continue a parked turn: release its handlers, plus any new user input.

        Input written before the results is already queued when they arrive, so
        the CLI sends both in one request (verified with 2.1.283).
        """
        blocks = _blocks(message)
        other = [b for b in blocks if b.get("type") != "tool_result"]
        if other:
            await self._write(other)
        for block in blocks:
            if block.get("type") == "tool_result":
                self._delivered.add(block["tool_use_id"])
                future = self._slot(block["tool_use_id"])
                if not future.done():
                    future.set_result(block)
        self.open_tool_ids = ()
        self.complete = False

    async def send(self, message: dict) -> None:
        if self.open_tool_ids:
            await self.answer(message)
        else:
            await self.start(_blocks(message))

    # Output

    async def _pump(self) -> None:
        """Drain the SDK (its buffer holds only 100 messages) and track the turn."""
        from claude_agent_sdk.types import (
            AssistantMessage,
            ResultMessage,
            StreamEvent,
            SystemMessage,
            UserMessage,
        )

        error: Exception = ClaudeProcessError(self.config.model, "Claude Code stopped.")
        stop_reason = None
        try:
            async for message in self._client.receive_messages():
                if getattr(message, "parent_tool_use_id", None):
                    continue
                if isinstance(message, StreamEvent):
                    event = message.event
                    kind = event.get("type")
                    if kind == "message_start":
                        stop_reason = None
                    elif kind == "message_delta":
                        stop_reason = (event.get("delta") or {}).get("stop_reason") or stop_reason
                    elif kind == "message_stop" and stop_reason != "tool_use":
                        # The turn ends here; absorb its result message.
                        self._final = True
                    self._queue.put_nowait(("event", event, self._last_uuid))
                elif isinstance(message, AssistantMessage):
                    self._last_uuid = message.uuid or self._last_uuid
                    if message.error:
                        text = " ".join(getattr(b, "text", "") for b in message.content)
                        self._api_error = (message.error, text.strip())
                elif isinstance(message, UserMessage):
                    self._check_results(message)
                elif isinstance(message, ResultMessage):
                    self.cli_session_id = message.session_id or self.cli_session_id
                    final, self._final = self._final, False
                    if message.is_error:
                        self.dead = True
                    self._turn_done.set()
                    # A result after the final message is expected; any other
                    # ends the turn without one, which a waiting reader must hear.
                    if not final or message.is_error:
                        self._queue.put_nowait(("result", message, None))
                elif isinstance(message, SystemMessage) and message.subtype == "init":
                    self.cli_session_id = message.data.get("session_id") or self.cli_session_id
        except asyncio.CancelledError:
            raise
        except Exception as cause:
            error = ClaudeProcessError(self.config.model, f"Claude Code stopped: {cause}")
            error.__cause__ = cause
        self.dead = True
        self._turn_done.set()
        self._queue.put_nowait(("error", error, None))

    def _check_results(self, message) -> None:
        """Notice the CLI answering one of pcode's calls itself (a refused input).

        Its transcript then no longer matches pcode's history, so retire it; the
        next request forks from the last shared message instead.
        """
        content = message.content if isinstance(message.content, list) else []
        for block in content:
            tool_use_id = getattr(block, "tool_use_id", None)
            if tool_use_id and tool_use_id not in self._delivered and not self._closed:
                logger.debug("Claude Code answered tool call %s itself", tool_use_id)
                self.dead = True

    async def response(self) -> AsyncIterator[dict]:
        """Raw stream events of the CLI's next assistant message, through message_stop."""
        tool_ids: list[str] = []
        stop_reason = None
        while True:
            kind, value, uuid = await self._queue.get()
            if kind == "error":
                self._queue.put_nowait((kind, value, uuid))  # every later reader fails too
                raise value
            if kind == "result":
                self.dead = True
                raise self._result_error(value)
            event_type = value.get("type")
            if event_type == "content_block_start":
                block = value.get("content_block") or {}
                if block.get("type") == "tool_use":
                    tool_ids.append(block.get("id"))
                    name = str(block.get("name", "")).removeprefix(TOOL_PREFIX)
                    value = {**value, "content_block": {**block, "name": name}}
            elif event_type == "message_delta":
                stop_reason = (value.get("delta") or {}).get("stop_reason") or stop_reason
            elif event_type == "message_stop":
                # Settled before handing it over, in case the reader stops here.
                self.open_tool_ids = tuple(tool_ids) if stop_reason == "tool_use" else ()
                self.response_uuid = uuid
                self.complete = True
                yield value
                return
            yield value

    def _result_error(self, result) -> Exception:
        kind, text = self._api_error or (None, "")
        self._api_error = None
        text = text or result.result or "; ".join(result.errors or []) or result.subtype
        status = result.api_error_status or ERROR_STATUS.get(kind or "")
        if status:
            body = {"type": "error", "error": {"type": kind or result.subtype, "message": text}}
            return ClaudeHTTPError(status, self.config.model, body)
        return ModelAPIError(self.config.model, f"Claude Code ended the turn: {text}")

    # Matching

    def accepts(self, config: SessionConfig, messages: list[dict], chain: list[str]) -> bool:
        """Whether `messages` is this transcript plus exactly one new user message."""
        known = len(self.chain)
        if self.busy or self.dead or not known or config != self.config:
            return False
        if len(messages) != known + 1 or chain[known - 1] != self.chain[-1]:
            return False
        message = messages[known]
        return message["role"] == "user" and _result_ids(message) == set(self.open_tool_ids)


# --- Pool ----------------------------------------------------------------------


@dataclass
class Checkout:
    session: ClaudeSession
    route: str  # "live", "fork" or "fresh", for logs and tests


class SessionPool:
    """Live processes for one event loop, matched to requests by history."""

    def __init__(self, index: ResumeIndex, factory=None) -> None:
        self.index = index
        self.factory = factory
        self.sessions: list[ClaudeSession] = []
        self._lock = asyncio.Lock()
        self._closing: set[asyncio.Task] = set()
        self._expiry: asyncio.TimerHandle | None = None

    async def checkout(
        self, config: SessionConfig, messages: list[dict], chain: list[str]
    ) -> Checkout:
        """A session that has been sent `messages` and is producing the next response."""
        await self.index.load()
        async with self._lock:
            for session in sorted(self.sessions, key=lambda s: s.last_used, reverse=True):
                if session.accepts(config, messages, chain):
                    session.busy = True
                    route, point, start = "live", None, len(session.chain)
                    break
            else:
                session = None
                point, start = self._fork_point(config, messages, chain)
                route = "fork" if point else "fresh"
        if session is None:
            session = await self._open(config, point)
            if session is None:  # the transcript to fork is gone
                route, point, start = "fresh", None, 0
                session = await self._open(config, None)
        delta = messages[start:]
        try:
            if route == "live":
                await session.send(delta[0])
            else:
                open_ids = _tool_use_ids(messages[start - 1]) if start else ()
                if len(delta) == 1 and _result_ids(delta[0]) == set(open_ids):
                    await session.start(_blocks(delta[0]))
                else:
                    await session.start(replay(delta, open_ids))
        except BaseException:
            self.release(session, ok=False)
            raise
        session.chain = chain[: len(messages)]
        logger.debug("claude request via %s session (%d new messages)", route, len(delta))
        return Checkout(session, route)

    def _fork_point(self, config, messages, chain) -> tuple[ForkPoint | None, int]:
        for position in range(len(messages) - 2, -1, -1):
            if messages[position]["role"] != "assistant":
                continue
            point = self.index.get(chain[position])
            if point is not None and point.cwd == config.cwd:
                return point, position + 1
        return None, 0

    async def _open(self, config: SessionConfig, point: ForkPoint | None) -> ClaudeSession | None:
        session = ClaudeSession(config, point)
        session.busy = True
        self.sessions.append(session)
        try:
            await session.connect(self.factory)
        except ClaudeStartError:
            self._drop(session)
            if point is not None:
                logger.debug("could not resume Claude session %s", point.session_id)
                return None
            raise
        except BaseException:
            self._drop(session)
            raise
        return session

    def record(self, session: ClaudeSession, assistant_hash: str) -> None:
        """Note the response just produced, so the history can continue or fork from it."""
        session.chain.append(assistant_hash)
        if session.cli_session_id and session.response_uuid:
            point = ForkPoint(session.cli_session_id, session.response_uuid, session.config.cwd)
            self.index.add(assistant_hash, point)

    def release(self, session: ClaudeSession, *, ok: bool) -> None:
        session.busy = False
        session.last_used = time.monotonic()
        if not ok:
            session.dead = True
        idle = [s for s in self.sessions if not s.busy]
        for stale in [s for s in idle if s.dead]:
            self._drop(stale)
        # Finished turns go first: a parked one is usually a parent waiting
        # on a delegation, and will be asked to continue.
        idle = sorted(
            (s for s in idle if not s.dead), key=lambda s: (bool(s.open_tool_ids), s.last_used)
        )
        for stale in idle[: max(0, len(idle) - MAX_IDLE_SESSIONS)]:
            self._drop(stale)
        if self._expiry is not None:
            self._expiry.cancel()
        self._expiry = asyncio.get_running_loop().call_later(IDLE_SECONDS, self._expire)

    def _expire(self) -> None:
        self._expiry = None
        cutoff = time.monotonic() - IDLE_SECONDS
        for session in [s for s in self.sessions if not s.busy and s.last_used <= cutoff]:
            self._drop(session)

    def _drop(self, session: ClaudeSession) -> None:
        with suppress(ValueError):
            self.sessions.remove(session)
        task = asyncio.get_running_loop().create_task(session.close())
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def aclose(self) -> None:
        if self._expiry is not None:
            self._expiry.cancel()
        for session in list(self.sessions):
            self._drop(session)
        if self._closing:
            await asyncio.gather(*self._closing, return_exceptions=True)


_pools: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, SessionPool]" = (
    weakref.WeakKeyDictionary()
)
_index: ResumeIndex | None = None


def pool() -> SessionPool:
    """This event loop's pool; processes cannot be shared across loops."""
    global _index
    loop = asyncio.get_running_loop()
    current = _pools.get(loop)
    if current is None:
        _index = _index or ResumeIndex()
        current = _pools[loop] = SessionPool(_index)
    return current


# --- Model ---------------------------------------------------------------------


class ClaudeProvider(AnthropicProvider):
    """Names responses `claude`. Its client is never called: the CLI makes every request."""

    @property
    def name(self) -> str:
        return "claude"

    def __init__(self) -> None:
        super().__init__(
            anthropic_client=AsyncAnthropic(
                api_key="unused", base_url="https://claude-code.invalid", max_retries=0
            )
        )


class _Events:
    """Stream events as the Anthropic SDK types `_process_streamed_response` reads."""

    def __init__(self, events: AsyncIterator[dict]) -> None:
        self._events = events

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = await self._events.__anext__()
        return construct_type(type_=BetaRawMessageStreamEvent, value=event)

    async def close(self) -> None:
        await self._events.aclose()


class ClaudeModel(AnthropicModel):
    @cached_property
    def profile(self):
        # The CLI forwards only MCP tools, so no Anthropic server tool reaches
        # the model and web tools fall back to local ones. Like Meridian, wire
        # tool deferral is off: hidden tools are withheld until found.
        from pydantic_ai.profiles import merge_profile
        from pydantic_ai.profiles.anthropic import AnthropicModelProfile

        return merge_profile(
            super().profile,
            AnthropicModelProfile(
                supported_native_tools=frozenset(),
                tool_deferral_mode=None,
                tool_addition_mode=None,
                # The system prompt is fixed per process; mid-conversation
                # system text must travel as user text instead.
                supports_inline_system_prompts=False,
            ),
        )

    def session_config(self, system, tools, settings) -> SessionConfig:
        if isinstance(system, str):
            prompt = system
        else:
            prompt = "\n\n".join(block["text"] for block in system if block.get("text"))
        wire_tools = [
            {
                "name": tool["name"],
                "description": tool.get("description") or "",
                "input_schema": tool["input_schema"],
            }
            for tool in tools
            if "input_schema" in tool
        ]
        effort = settings.get("anthropic_effort")
        thinking = settings.get("anthropic_thinking")
        return SessionConfig(
            model=self.model_name,
            cwd=str(settings.get(CWD_SETTING) or os.getcwd()),
            system_prompt=prompt,
            tools=json.dumps(wire_tools, sort_keys=True),
            effort=effort if effort in EFFORTS else None,
            thinking=json.dumps(thinking, sort_keys=True) if isinstance(thinking, dict) else None,
        )

    async def request(self, messages, model_settings, model_request_parameters):
        async with self.request_stream(
            messages, model_settings, model_request_parameters
        ) as stream:
            async for _ in stream:
                pass
        return stream.get()

    async def count_tokens(self, messages, model_settings, model_request_parameters):
        raise NotImplementedError("claude: models cannot count tokens ahead of a request")

    @asynccontextmanager
    async def request_stream(
        self,
        messages,
        model_settings,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncIterator[StreamedResponse]:
        check_allow_model_requests()
        settings, parameters = self.prepare_request(model_settings, model_request_parameters)
        settings = dict(settings or {})
        system, mapped = await self._map_message(messages, parameters, settings)
        tools, _ = self._prepare_tools_and_tool_choice(settings, parameters)
        config = self.session_config(system, tools, settings)
        mapped = normalize(mapped)
        chain = lineage(mapped)
        sessions = pool()
        checkout = await sessions.checkout(config, mapped, chain)
        session = checkout.session
        ok = False
        try:
            events = _Events(session.response())
            stream = await self._process_streamed_response(events, parameters, settings)
            yield stream
            # Anything short of the whole message leaves the process mid-turn.
            # A whole one is a fork point even if the process has since died.
            if session.complete:
                _, answer = await self._map_message([stream.get()], parameters, settings)
                answer = normalize(answer)
                if len(answer) == 1 and answer[0]["role"] == "assistant":
                    sessions.record(session, _digest(chain[-1], answer[0]))
                    ok = not session.dead
        finally:
            sessions.release(session, ok=ok)


def claude_model(model: str) -> ClaudeModel:
    name = model.removeprefix(PREFIX)
    if not name.strip():
        raise ValueError("Claude requires a model ID: claude:<model-id>")
    return ClaudeModel(name, provider=ClaudeProvider())


class ClaudeWorkspace(AbstractCapability):
    """Run `claude:` requests' CLI in the agent's workspace.

    The CLI tells the model its working directory, and keeps transcripts per
    directory; pcode's process directory is not the workspace in worktree mode.
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)

    async def before_model_request(
        self, ctx: RunContext, request_context: ModelRequestContext
    ) -> ModelRequestContext:
        if request_context.model.system != "claude":
            return request_context
        settings = {**(request_context.model_settings or {}), CWD_SETTING: str(self.workspace)}
        return replace(request_context, model_settings=settings)
