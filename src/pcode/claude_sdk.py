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
as text, as Meridian does. See dev/anthropic-providers.md.

pcode holds no credentials here: the CLI signs in itself (`/login claude`).
"""

import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import sys
import time
import weakref
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, replace
from functools import cached_property
from pathlib import Path
from tempfile import NamedTemporaryFile, mkstemp
from typing import Any

from anthropic import AsyncAnthropic
from anthropic._models import construct_type
from anthropic.types.beta import BetaRawMessageStreamEvent
from pydantic_ai import RunContext
from pydantic_ai._utils import PeekableAsyncStream
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
# Each process holds about 110-135 MB of its own beyond the ~200 MB binary the
# processes share (measured with vmmap), and forking a transcript is warm anyway,
# so keeping one only saves the ~0.8 s start. Finished ones are capped and expire
# soon; one is enough for the next turn to continue on. A parked one belongs to a
# run still executing its tools (often a parent waiting on delegations), so it is
# never capped, only expired, and later. Under memory pressure none are kept.
# The first two are defaults for `claude_idle_processes` / `claude_idle_minutes`.
MAX_IDLE_SESSIONS = 1
IDLE_SECONDS = 10 * 60
PARKED_SECONDS = 30 * 60
# How often idle processes are checked against memory pressure, and what counts:
# less than this share of physical memory available.
PRESSURE_CHECK_SECONDS = 60.0
LOW_MEMORY_FRACTION = 0.10
CLOSE_TIMEOUT_SECONDS = 5.0
# A finished turn's result follows its last message within milliseconds.
TURN_END_TIMEOUT_SECONDS = 60.0
# The CLI calls a tool's handler as the tool_use block ends; allow for a slow
# machine before deciding it never will (a call it refused or could not parse).
CALL_TIMEOUT_SECONDS = 10.0
# `connect` returns before the CLI has listed pcode's tools; a message sent in
# that gap goes out with none (verified with 2.1.285), so wait for them.
TOOLS_READY_TIMEOUT_SECONDS = 30.0
TOOLS_READY_POLL_SECONDS = 0.02
INDEX_LIMIT = 4000
MISSING_SDK = (
    "claude: models need pcode's optional `claude` extra, which is not installed. "
    "From a pcode checkout run `make install`, or `uv tool install --editable '.[claude]'`."
)
REPLAY_INTRO = (
    "This conversation began outside the current session, so its earlier messages are "
    "replayed below as a transcript. Treat them as having happened here and continue "
    "from the final message.\n\n"
)

# The child inherits pcode's environment. An empty value is unset to the CLI
# (verified: requests then bill the subscription), so neither pcode's own
# Anthropic key, token or endpoint nor a cloud route exported for other Claude
# Code use can silently redirect or bill a `claude:` request.
LOGIN_ENV = {
    "ANTHROPIC_API_KEY": "",
    "ANTHROPIC_AUTH_TOKEN": "",
    "ANTHROPIC_BASE_URL": "",
    "CLAUDE_CODE_USE_BEDROCK": "",
    "CLAUDE_CODE_USE_VERTEX": "",
    "CLAUDE_CODE_USE_FOUNDRY": "",
    "CLAUDE_CODE_USE_GATEWAY": "",
    "CLAUDE_CODE_USE_MANTLE": "",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS": "",
    "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD": "",
}
CLI_ENV = {
    **LOGIN_ENV,
    # pcode owns retries (visible in the UI), compaction and tool deferral. A
    # transcript the CLI compacted itself would no longer match pcode's history.
    "CLAUDE_CODE_MAX_RETRIES": "0",
    "DISABLE_AUTO_COMPACT": "1",
    "DISABLE_COMPACT": "1",
    "ENABLE_TOOL_SEARCH": "false",
    # pcode already bounds tool output; never let the CLI truncate it again.
    "MAX_MCP_OUTPUT_TOKENS": "1000000",
    # A subscription login defaults to 1-hour cache writes (2x input, against
    # 1.25x). pcode's tool loops send requests well inside five minutes, so the
    # hour rarely pays off (dev/anthropic-providers.md). This variable wins
    # over ENABLE_PROMPT_CACHING_1H; only FORCE_PROMPT_CACHING_5M outranks it.
    "CLAUDE_CODE_PROMPT_CACHE_TTL": "5m",
    # A parked handler lasts as long as the tool runs, delegations included.
    "MCP_TOOL_TIMEOUT": str(7 * 24 * 3600 * 1000),
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_AUTOUPDATER": "1",
}
# Statuses for the CLI's error kinds when it reports no HTTP status itself.
# `server_error` has none on purpose: without a status it is a connection failure
# (`ClaudeConnectionError`), while a real 5xx or 529 always carries its status.
ERROR_STATUS = {
    "authentication_failed": 401,
    "rate_limit": 429,
    "invalid_request": 400,
}


class ClaudeHTTPError(ModelHTTPError):
    """An API error the CLI reported for a `claude:` request."""


class ClaudeProcessError(ModelAPIError):
    """The CLI process ended mid-request; a retry starts another (transient)."""


class ClaudeConnectionError(ModelAPIError):
    """The CLI's request got no HTTP response: dropped, refused or timed out (transient).

    Verified against the bundled CLI: all three arrive as a `server_error` with
    no `api_error_status`, where a real 500 or 529 carries its status. With the
    CLI's own retries off, pcode's transient retry covers these, as it covers
    the same failures on its other providers.
    """


class ClaudeStartError(ModelAPIError):
    """The CLI process could not be started or resumed."""


class ClaudeSDKMissing(ValueError):
    """pcode was installed without its `claude` extra."""


def cli_path() -> str | None:
    """The CLI the SDK runs: its bundled binary, else `claude` on PATH."""
    import shutil

    import claude_agent_sdk

    name = "claude.exe" if sys.platform == "win32" else "claude"
    bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / name
    return str(bundled) if bundled.is_file() else shutil.which("claude")


def failure_hint(error: BaseException) -> str | None:
    """What to do about a failed `claude:` request, or None for any other provider."""
    seen = set()
    while error is not None and id(error) not in seen and len(seen) < 16:
        seen.add(id(error))
        if isinstance(error, ClaudeSDKMissing):
            return MISSING_SDK
        if isinstance(error, ClaudeConnectionError):
            # The CLI's own text stays in the diagnostics log.
            return (
                "Claude Code got no response from Anthropic (connection dropped, refused "
                "or timed out). Check network/proxy connectivity and retry when ready."
            )
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
    """Drop cache markers and make binary sources plain base64 JSON.

    The CLI places its own cache markers, and they never change content.
    Pydantic AI maps image and PDF bytes to `io.BytesIO`, which the Anthropic
    SDK encodes on send; the CLI takes JSON, and hashing needs stable values.
    """
    if isinstance(value, io.BytesIO):
        return base64.b64encode(value.getvalue()).decode("ascii")
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
        self._loaded: dict[str, ForkPoint] | None = None

    def _file(self) -> Path:
        return self.path or index_path()

    @property
    def _entries(self) -> dict[str, ForkPoint]:
        # Read once per process, synchronously: at most INDEX_LIMIT short lines,
        # and no await in between means no second reader can interleave.
        if self._loaded is None:
            self._loaded = self._load()
        return self._loaded

    def _load(self) -> dict[str, ForkPoint]:
        entries: dict[str, ForkPoint] = {}
        try:
            lines = self._file().read_text().splitlines()
        except OSError:
            return entries
        for line in lines:
            try:
                row = json.loads(line)
                if row.get("dropped"):
                    entries.pop(row["key"], None)
                    continue
                entries[row["key"]] = ForkPoint(row["session"], row["uuid"], row["cwd"])
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
        if len(lines) > INDEX_LIMIT:
            kept = list(entries.items())[-INDEX_LIMIT // 2 :]
            entries = dict(kept)
            temporary = None
            try:
                with NamedTemporaryFile(
                    "w", dir=self._file().parent, suffix=".tmp", delete=False
                ) as file:
                    temporary = Path(file.name)
                    file.write("".join(self._line(k, p) for k, p in kept))
                temporary.replace(self._file())
            except OSError:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return entries

    @staticmethod
    def _line(key: str, point: ForkPoint) -> str:
        row = {"key": key, "session": point.session_id, "uuid": point.uuid, "cwd": point.cwd}
        return json.dumps(row) + "\n"

    def get(self, key: str) -> ForkPoint | None:
        return self._entries.get(key)

    def forget(self, session_id: str) -> None:
        """Stop offering a transcript that could not be resumed, for this process."""
        for key in [k for k, p in self._entries.items() if p.session_id == session_id]:
            del self._entries[key]

    def drop(self, keys: list[str]) -> None:
        """Never fork these histories again, in any pcode process."""
        dropped = [key for key in keys if self._entries.pop(key, None) is not None]
        self._append("".join(json.dumps({"key": key, "dropped": True}) + "\n" for key in dropped))

    def add(self, key: str, point: ForkPoint) -> None:
        if self._entries.get(key) == point:
            return
        self._entries[key] = point
        self._append(self._line(key, point))

    def _append(self, lines: str) -> None:
        if not lines:
            return
        try:
            self._file().parent.mkdir(parents=True, exist_ok=True)
            with self._file().open("a") as file:
                file.write(lines)
        except OSError:
            logger.debug("could not update the Claude fork index", exc_info=True)


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
    # pcode's output ceiling; the CLI clamps it to the model's own limit.
    max_tokens: int | None = None

    def options(self, server, resume: ForkPoint | None, stderr, prompt_file: str) -> Any:
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
            system_prompt={"type": "file", "path": prompt_file},
            include_partial_messages=True,
            model=self.model,
            cwd=self.cwd,
            env={
                **CLI_ENV,
                **(
                    {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(self.max_tokens)}
                    if self.max_tokens
                    else {}
                ),
            },
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


class _Diverged(ClaudeProcessError):
    """The CLI's transcript stopped matching pcode's history (it acted on its own)."""

    live = False  # raised while continuing a live process, which a fork can replace


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
        # Whether it called a tool by a name the CLI does not offer (pcode's bare
        # name). The CLI refuses such a call, and a transcript that holds one
        # teaches the model to repeat it, so it must never be forked.
        self.stray_call = False
        self.busy = False
        self.dead = False
        self.last_used = time.monotonic()
        # Set by the pool: stop an idle process that went its own way.
        self.retire: Callable[[], None] | None = None
        self._client: Any = None
        self._prompt_file: str | None = None
        self._pump_task: asyncio.Task | None = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._slots: dict[str, asyncio.Future] = {}
        # Calls the CLI parked with pcode, and results it got from pcode (through
        # a released handler, or sent as input). Any other result is its own.
        self._called: set[str] = set()
        self._answered: set[str] = set()
        # Pulsed on every change a waiter may be waiting for.
        self._changed = asyncio.Event()
        self._last_uuid: str | None = None
        # Whether pcode asked for the next message; anything else is the CLI's own.
        self._expecting = False
        self._final = False
        self._turn_done = asyncio.Event()
        self._turn_done.set()
        self._api_error: tuple[str | None, str] | None = None
        self._stderr: deque[str] = deque(maxlen=20)
        self._closed = False

    # Lifecycle

    async def connect(self, factory=None) -> None:
        # A file, not argv: a large prompt would pass Linux's 128 KiB argument
        # limit, and argv is readable by every local user. mkstemp makes it 0600.
        descriptor, self._prompt_file = mkstemp(prefix="pcode-claude-", suffix=".md")
        with os.fdopen(descriptor, "w") as file:
            file.write(self.config.system_prompt)
        options = self.config.options(
            self._server(), self.resume, self._stderr.append, self._prompt_file
        )
        self._client = (factory or _client_factory)(options)
        try:
            await self._client.connect()
        except Exception as error:
            self.dead = True
            self._remove_prompt_file()
            raise ClaudeStartError(
                self.config.model, f"Claude Code could not start: {self._detail(error)}"
            ) from error
        if not self._closed:
            try:
                await self._tools_ready()
            except BaseException as error:
                stopped, self._closed, self.dead = self._closed, True, True
                self._remove_prompt_file()
                with suppress(Exception):
                    await self._client.disconnect()
                if stopped and isinstance(error, Exception):
                    message = "Claude Code was stopped."
                    raise ClaudeProcessError(self.config.model, message) from error
                raise
        if self._closed:  # closed while connecting: `close` found nothing to stop
            with suppress(Exception):
                await self._client.disconnect()
            raise ClaudeProcessError(self.config.model, "Claude Code was stopped.")
        self._pump_task = asyncio.create_task(self._pump(), name="claude-sdk-pump")

    async def _tools_ready(self) -> None:
        """Wait until the CLI offers every pcode tool to the model.

        A request sent before then declares no tools, so the model calls them
        by the bare names in pcode's prompt, which the CLI refuses.
        """
        expected = len(json.loads(self.config.tools))
        state = "not listed"
        try:
            async with asyncio.timeout(TOOLS_READY_TIMEOUT_SECONDS):
                while True:
                    status = await self._client.get_mcp_status()
                    server = next(
                        (s for s in status.get("mcpServers") or [] if s.get("name") == SERVER),
                        None,
                    )
                    if server is not None:
                        state = str(server.get("status"))
                        if state == "connected" and len(server.get("tools") or []) >= expected:
                            return
                        if state in ("failed", "needs-auth", "disabled"):
                            break
                    await asyncio.sleep(TOOLS_READY_POLL_SECONDS)
        except TimeoutError:
            pass
        raise ClaudeProcessError(
            self.config.model, f"Claude Code did not load pcode's tools ({state})."
        )

    async def close(self) -> None:
        """Stop the process without letting it act on its own first.

        A process closed while parked records the missing results as errors and
        carries on (verified: two more billed requests), so interrupt the turn.
        """
        if self._closed:
            return
        self._closed = self.dead = True
        self._changed.set()
        # A reader still waiting (pcode exiting mid-request) must not wait forever.
        self._queue.put_nowait(
            ("error", ClaudeProcessError(self.config.model, "Claude Code was stopped."), None)
        )
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
        self._remove_prompt_file()

    def _remove_prompt_file(self) -> None:
        if self._prompt_file is not None:
            with suppress(OSError):
                os.unlink(self._prompt_file)
            self._prompt_file = None

    def _detail(self, error: BaseException | None = None) -> str:
        from pcode.diagnostics import redact

        detail = "; ".join(list(self._stderr)[-3:]) or (str(error) if error else "")
        return redact(detail or (type(error).__name__ if error else "no detail"))[:500]

    def _diverge(self, why: str) -> None:
        """Retire a process whose transcript no longer matches pcode's history."""
        if self.dead:
            return
        logger.debug("Claude Code session diverged: %s", why)
        self.dead = True
        self._changed.set()
        if self._expecting:
            # A reader waits for the answer to its input, but what the CLI sends
            # next follows its own history: drop it and fail the reader (the
            # runtime's retry forks from the last shared message).
            self._expecting = False
            error = ClaudeProcessError(self.config.model, f"Claude Code went its own way: {why}.")
            self._queue.put_nowait(("error", error, None))
        if not self.busy and self.retire is not None:
            self.retire()  # interrupts whatever it started on its own

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
                self._diverge("a tool call without an id")
                return types.CallToolResult(
                    content=[
                        types.TextContent(type="text", text="pcode could not match this call.")
                    ],
                    isError=True,
                )
            self._called.add(tool_use_id)
            self._changed.set()
            # Parked until pcode runs the tool and sends its result.
            try:
                block = await self._slot(tool_use_id)
            finally:
                self._slots.pop(tool_use_id, None)  # never hold results once answered
            self._answered.add(tool_use_id)
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

    async def _until(self, condition: Callable[[], bool]) -> None:
        while not condition():
            self._changed.clear()
            await self._changed.wait()

    def _ask(self) -> None:
        """Expect the CLI's next message; raise if it has already gone its own way."""
        if self.dead:
            raise _Diverged(self.config.model, "Claude Code continued on its own.")
        self._expecting = True
        self.complete = False

    async def start(self, content: list[dict]) -> None:
        """Begin a turn with a new user message."""
        try:
            async with asyncio.timeout(TURN_END_TIMEOUT_SECONDS):
                # Or until it goes its own way, which then need not run to the end.
                await self._until(lambda: self._turn_done.is_set() or self.dead)
        except TimeoutError:
            raise _Diverged(self.config.model, "Claude Code did not end its turn.") from None
        self._ask()
        self._turn_done.clear()
        # A fork may start with results for the calls its transcript ends on.
        self._answered.update(b["tool_use_id"] for b in content if b.get("type") == "tool_result")
        await self._write(content)

    async def answer(self, message: dict) -> None:
        """Continue a parked turn: release its handlers, plus any new user input.

        The CLI parks a call as its tool_use block ends. One that never arrives
        was refused or unparseable, and the CLI has answered it itself.
        Input written before the results is already queued when they arrive, so
        the CLI sends both in one request (verified with 2.1.283).
        """
        try:
            async with asyncio.timeout(CALL_TIMEOUT_SECONDS):
                await self._until(lambda: bool(self._called & set(self.open_tool_ids)) or self.dead)
        except TimeoutError:
            self._diverge("no tool call reached pcode")
        self._ask()
        blocks = _blocks(message)
        other = [b for b in blocks if b.get("type") != "tool_result"]
        if other:
            await self._write(other)
        for block in blocks:
            if block.get("type") == "tool_result":
                future = self._slot(block["tool_use_id"])
                if not future.done():
                    future.set_result(block)
        self.open_tool_ids = ()

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

        error: Exception | None = None
        stop_reason = None
        wanted = False
        try:
            async for message in self._client.receive_messages():
                if getattr(message, "parent_tool_use_id", None):
                    continue
                if isinstance(message, StreamEvent):
                    event = message.event
                    kind = event.get("type")
                    if kind == "message_start":
                        stop_reason = None
                        wanted, self._expecting = self._expecting, False
                        if not wanted:
                            # A request of its own (a nudge after a thinking-only
                            # reply, output-limit recovery, a retried tool call).
                            self._diverge("a message pcode did not ask for")
                    elif kind == "message_delta":
                        stop_reason = (event.get("delta") or {}).get("stop_reason") or stop_reason
                    elif kind == "message_stop" and stop_reason != "tool_use":
                        # The turn ends here; absorb its result message.
                        self._final = True
                    if wanted:
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
                    self._changed.set()
                    # A result after the final message is expected; any other
                    # ends the turn without one, which a waiting reader must hear.
                    if not final or message.is_error:
                        self._queue.put_nowait(("result", message, None))
                elif isinstance(message, SystemMessage) and message.subtype == "init":
                    self.cli_session_id = message.data.get("session_id") or self.cli_session_id
        except asyncio.CancelledError:
            raise
        except Exception as cause:
            error = cause
        self.dead = True
        self._turn_done.set()
        self._changed.set()
        stopped = ClaudeProcessError(
            self.config.model, f"Claude Code stopped: {self._detail(error)}"
        )
        stopped.__cause__ = error
        self._queue.put_nowait(("error", stopped, None))

    def _check_results(self, message) -> None:
        """Notice the CLI answering one of pcode's calls itself (a refused input).

        Checked against results pcode actually handed over, not ones it merely
        sent: with calls made one at a time, the CLI can refuse a later call
        after pcode has sent every result.
        """
        content = message.content if isinstance(message.content, list) else []
        for block in content:
            tool_use_id = getattr(block, "tool_use_id", None)
            if tool_use_id and tool_use_id not in self._answered and not self._closed:
                self._diverge(f"Claude Code answered tool call {tool_use_id} itself")

    async def response(self) -> AsyncIterator[dict]:
        """Raw stream events of the CLI's next assistant message, through message_stop."""
        tool_ids: list[str] = []
        stop_reason = None
        self.stray_call = False
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
                    wire = str(block.get("name", ""))
                    self.stray_call = self.stray_call or not wire.startswith(TOOL_PREFIX)
                    name = wire.removeprefix(TOOL_PREFIX)
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
        if kind == "server_error" and not result.api_error_status:
            return ClaudeConnectionError(self.config.model, text)
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
        for attempt in (1, 2):
            try:
                return await self._checkout(config, messages, chain)
            except _Diverged as error:
                # A live process that had gone its own way is retired: fork now.
                if error.live and attempt == 1:
                    continue
                # Internal only: the runtime retries this name as transient.
                raise ClaudeProcessError(config.model, error.message) from error
        raise AssertionError("unreachable")

    async def _checkout(self, config, messages, chain) -> Checkout:
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
                single = len(delta) == 1 and delta[0]["role"] == "user"
                if single and _result_ids(delta[0]) == set(open_ids):
                    await session.start(_blocks(delta[0]))
                else:
                    await session.start(replay(delta, open_ids))
        except _Diverged as error:
            error.live = route == "live"
            self.release(session, ok=False)
            raise
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
        session.retire = lambda: self._drop(session)
        self.sessions.append(session)
        try:
            await session.connect(self.factory)
        except ClaudeStartError:
            self._drop(session)
            if point is not None:
                logger.debug("could not resume Claude session %s", point.session_id)
                self.index.forget(point.session_id)
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
        low = memory_low()
        for stale in [s for s in self.sessions if not s.busy and (s.dead or low)]:
            self._drop(stale)
        finished = sorted(
            (s for s in self.sessions if not s.busy and not s.open_tool_ids),
            key=lambda s: s.last_used,
        )
        for stale in finished[: max(0, len(finished) - _idle_limit())]:
            self._drop(stale)
        self._schedule_expiry()

    @staticmethod
    def _deadline(session: ClaudeSession) -> float:
        return session.last_used + (PARKED_SECONDS if session.open_tool_ids else _idle_seconds())

    def _schedule_expiry(self) -> None:
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None
        idle = [s for s in self.sessions if not s.busy]
        if idle:
            delay = max(0.0, min(map(self._deadline, idle)) - time.monotonic())
            delay = min(delay, PRESSURE_CHECK_SECONDS)
            self._expiry = asyncio.get_running_loop().call_later(delay, self._expire)

    def _expire(self) -> None:
        self._expiry = None
        now = time.monotonic()
        low = memory_low()
        for session in [
            s for s in self.sessions if not s.busy and (low or self._deadline(s) <= now)
        ]:
            self._drop(session)
        self._schedule_expiry()

    def _drop(self, session: ClaudeSession) -> None:
        with suppress(ValueError):
            self.sessions.remove(session)
        task = asyncio.get_running_loop().create_task(session.close())
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def aclose(self) -> None:
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None
        for session in list(self.sessions):
            self._drop(session)
        if self._closing:
            await asyncio.gather(*self._closing, return_exceptions=True)


def _preference(key: str) -> int | None:
    """A saved whole-number preference, read on use so `/config set` applies at once."""
    from pcode.preferences import load_preferences

    value = load_preferences().get(key, "")
    return int(value) if value.isdecimal() else None


def memory_low() -> bool:
    """Whether the machine is short of memory, so no idle process is worth keeping.

    Dropping one costs the next request a warm fork, even mid-round: the tool
    results then arrive structured at the fork point.
    """
    try:
        import psutil

        memory = psutil.virtual_memory()
    except Exception:
        return False
    return memory.available < memory.total * LOW_MEMORY_FRACTION


def _idle_limit() -> int:
    value = _preference("claude_idle_processes")
    return MAX_IDLE_SESSIONS if value is None else value


def _idle_seconds() -> float:
    return (_preference("claude_idle_minutes") or 0) * 60 or IDLE_SECONDS


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


async def shutdown() -> None:
    """Stop this event loop's CLI processes, interrupting any mid-turn first.

    Called as pcode exits; the SDK's own atexit hook only sends SIGTERM.
    """
    current = _pools.pop(asyncio.get_running_loop(), None)
    if current is not None:
        await current.aclose()


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
        max_tokens = settings.get("max_tokens")
        return SessionConfig(
            model=self.model_name,
            cwd=str(settings.get(CWD_SETTING) or os.getcwd()),
            system_prompt=prompt,
            tools=json.dumps(wire_tools, sort_keys=True),
            effort=effort if effort in EFFORTS else None,
            thinking=json.dumps(thinking, sort_keys=True) if isinstance(thinking, dict) else None,
            max_tokens=max_tokens if isinstance(max_tokens, int) and max_tokens > 0 else None,
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
            # `_process_streamed_response` peeks the first event before reading.
            events = PeekableAsyncStream(_Events(session.response()))
            stream = await self._process_streamed_response(events, parameters, settings)
            yield stream
            # Anything short of the whole message leaves the process mid-turn.
            # A whole one is a fork point even if the process has since died.
            if session.complete and session.stray_call:
                # Resuming any of this history would show the model its own
                # refused calls again: the next request replays it instead.
                sessions.index.drop(chain)
            elif session.complete:
                _, answer = await self._map_message([stream.get()], parameters, settings)
                answer = normalize(answer)
                if len(answer) == 1 and answer[0]["role"] == "assistant":
                    sessions.record(session, _digest(chain[-1], answer[0]))
                    ok = not session.dead
        finally:
            sessions.release(session, ok=ok)


def claude_model(model: str) -> ClaudeModel:
    from pcode.models import claude_sdk_installed

    name = model.removeprefix(PREFIX)
    if not name.strip():
        raise ValueError("Claude requires a model ID: claude:<model-id>")
    if not claude_sdk_installed():
        raise ClaudeSDKMissing(MISSING_SDK)
    return ClaudeModel(name, provider=ClaudeProvider())


class ClaudeWorkspace(AbstractCapability):
    """Run `claude:` requests' CLI in the agent's workspace.

    The CLI tells the model its working directory, and keeps transcripts per
    directory; pcode's process directory is not the workspace in worktree mode.
    A `fallback` one yields to any other: sub-agents get the parent's as a
    fallback, and an isolated worker's own checkout must win whatever the order.
    """

    def __init__(self, workspace: Path, *, fallback: bool = False) -> None:
        self.workspace = Path(workspace)
        self.fallback = fallback

    async def before_model_request(
        self, ctx: RunContext, request_context: ModelRequestContext
    ) -> ModelRequestContext:
        settings = request_context.model_settings or {}
        if request_context.model.system != "claude" or (self.fallback and CWD_SETTING in settings):
            return request_context
        settings = {**settings, CWD_SETTING: str(self.workspace)}
        return replace(request_context, model_settings=settings)
