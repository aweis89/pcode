"""`antigravity:` models: a Google AI Pro/Ultra subscription through `agy acp`.

pcode is an Agent Client Protocol front end here, the shape Google approves
for subscription use: the unmodified `agy acp` server owns the agent loop, its
tools and its sign-in, and pcode only sends the user's prompt and shows the
turn. One pcode request is one whole Antigravity turn. Its tool calls come back
as provider-executed (native) tool parts, and pcode's own tools are never
offered to it as function tools, so pcode's agent loop ends after every
response. (Delegation alone is offered the ACP way, as an MCP server
Antigravity's own loop may call: see `pcode.antigravity_tools`.) A request that
doesn't end in the user's own prompt (tool results after a mid-turn `/model`
switch, a bare continuation) is refused rather than sent: pcode must never
prompt Antigravity on its own. For the same reason `antigravity:` cannot be an
advisor, a delegation target, or a compaction summarizer (see `side_model` and
`compaction`). User-initiated `/btw` side questions are allowed: each runs in
its own `agy acp` session (`side=True`, with no delegation tools) replaying the
conversation as context, so it never blocks or pollutes the main session.

One `agy acp` process serves one conversation. Each response is stamped
`<session ID>#<turn>`, and a ledger in pcode's state directory records each
session's newest turn. A request whose history ends at a session's newest turn
continues it, in the live process or, after a restart, through
`session/resume`. Anything else (a rewind or branch, whose session holds turns
the history dropped, or a switch from another model) starts a new session with
the earlier conversation replayed as text.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import shutil
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelResponseStreamEvent,
    NativeToolCallPart,
    NativeToolReturnPart,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RequestUsage

from pcode.steering import steering_pending

PREFIX = "antigravity:"
SYSTEM = "antigravity"
COMMAND = "agy"
PROTOCOL_VERSION = 1
# Credentials and endpoints that would make `agy` bill an API key or a Cloud
# project, or send its requests somewhere else, instead of using the
# subscription it signed in with.
SCRUBBED_ENV = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS")
SCRUBBED_PREFIXES = ("GOOGLE_CLOUD_", "GOOGLE_GENAI_", "GOOGLE_VERTEX_", "GOOGLE_GEMINI_")
SIDE_REFUSAL = (
    "Antigravity runs only for prompts you send (the conversation or /btw): "
    "it can't be an advisor, a compaction summarizer, or a delegated worker. "
    "Pick another model for those."
)
OWN_PROMPTS_ONLY = (
    "Antigravity only answers prompts you send: it can't take over a turn already "
    "running on another model or continue one without a new message. Send a prompt."
)
AUTH_REQUIRED = -32000
LOGIN_HINT = "Sign in once by running `agy` (or `agy acp --login`) in a terminal."
REPLAY_INTRO = (
    "This conversation started elsewhere; its earlier messages are replayed below as a "
    "transcript. Treat them as having happened and continue from the final message."
)
SIDE_REPLAY_INTRO = (
    "You are answering a side question beside the main conversation, which may still be "
    "running in the same workspace. Its transcript so far is below for context; answer "
    "only the side question that follows and do not continue or redo the main task."
)
# How long a cancelled turn may take to report that it stopped.
CANCEL_GRACE = 10.0
# How often a running turn checks for the user's steering messages.
STEER_POLL = 0.25
# Quiet seconds that end a stretch of text Antigravity writes between turns.
IDLE_QUIET = 1.5
# Quiet seconds after a held turn's background work finishes and its follow-up
# reply closes (`usage_update`), in case Antigravity immediately starts another tool.
DEFERRED_FINALIZE_DEBOUNCE = 3.0
# Safety cap while waiting for an unsolicited follow-up reply's `usage_update`.
OPEN_REPLY_QUIET_LIMIT = 120.0
# Tool titles Antigravity leaves in_progress across `session/prompt` settlement.
SCHEDULE_TITLES = frozenset({"Run schedule", "Running schedule"})
# Sessions remembered in the ledger; older ones replay instead of resuming.
LEDGER_SIZE = 500


class AntigravityError(RuntimeError):
    """A failure `agy acp` reported, or a request pcode refuses to send, phrased for the user."""


def installed() -> bool:
    return shutil.which(COMMAND) is not None


def _state_dir() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return state / "pcode"


def models_cache() -> Path:
    return _state_dir() / "antigravity-models.json"


def _read_json(path: Path, kind: type) -> Any:
    try:
        value = json.loads(path.read_text())
    except OSError, ValueError:
        return kind()
    return value if isinstance(value, kind) else kind()


def _write_json(path: Path, value: object) -> None:
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        # Per process: two pcode processes must not share one temporary file.
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(value))
        temporary.replace(path)


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Hold an exclusive lock beside `path` for a read-modify-write across processes."""
    import fcntl

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path.with_name(path.name + ".lock"), "a")
    except OSError:
        yield  # Unlocked is still better than no ledger at all.
        return
    with handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def cached_models() -> tuple[str, ...]:
    """Model IDs the last Antigravity session offered, for the picker."""
    return tuple(name for name in _read_json(models_cache(), list) if isinstance(name, str))


def _offered(result: dict) -> list[str]:
    offered = (result.get("models") or {}).get("availableModels") or []
    return [m["modelId"] for m in offered if isinstance(m, dict) and m.get("modelId")]


def _remember_models(result: dict) -> None:
    names = _offered(result)
    if names and list(cached_models()) != names:
        _write_json(models_cache(), names)


def _windows_path() -> Path:
    return _state_dir() / "antigravity-windows.json"


# In-memory mirror of `_windows_path()`, keyed by path so rendering hits disk
# at most once per state dir and tests that swap `XDG_STATE_HOME` reload cleanly.
_windows_loaded_path: Path | None = None
_windows: dict[str, int] = {}


def _load_windows() -> dict[str, int]:
    global _windows_loaded_path, _windows
    from pcode.model_metadata import positive_int

    path = _windows_path()
    if _windows_loaded_path != path:
        raw = _read_json(path, dict)
        _windows = {
            name: size
            for name, value in raw.items()
            if isinstance(name, str) and (size := positive_int(value)) is not None
        }
        _windows_loaded_path = path
    return _windows


def cached_window(model_name: str) -> int | None:
    """Last context window size Antigravity reported for `model_name`, if any."""
    return _load_windows().get(model_name)


def _remember_window(model_name: str, size: int) -> None:
    from pcode.model_metadata import positive_int

    valid = positive_int(size)
    if not model_name or valid is None:
        return
    if _load_windows().get(model_name) == valid:
        return
    path = _windows_path()
    with _locked(path):
        stored = {
            name: value
            for name, value in _read_json(path, dict).items()
            if isinstance(name, str) and positive_int(value) is not None
        }
        stored[model_name] = valid
        _write_json(path, stored)
    _windows[model_name] = valid


def _ledger_path() -> Path:
    return _state_dir() / "antigravity-sessions.json"


def newest_turn(session_id: str) -> int | None:
    """The newest turn pcode prompted in this Antigravity session, if it knows."""
    turn = _read_json(_ledger_path(), dict).get(session_id)
    return turn if isinstance(turn, int) else None


def _record_turn(session_id: str, turn: int) -> None:
    # A lost update could put back an older turn and let a rewind resume a
    # session holding turns its history dropped, so writers take turns.
    with _locked(_ledger_path()):
        ledger = _read_json(_ledger_path(), dict)
        ledger.pop(session_id, None)
        ledger[session_id] = turn  # Last, so trimming drops the oldest.
        _write_json(_ledger_path(), dict(list(ledger.items())[-LEDGER_SIZE:]))


def child_env() -> dict[str, str]:
    return {
        k: v
        for k, v in os.environ.items()
        if k not in SCRUBBED_ENV and not k.startswith(SCRUBBED_PREFIXES)
    }


def _spawn(coroutine) -> asyncio.Task:
    """Run `coroutine` as a task kept alive until it finishes."""
    task = asyncio.ensure_future(coroutine)
    _background.add(task)
    task.add_done_callback(_background.discard)
    task.add_done_callback(lambda t: t.cancelled() or t.exception())
    return task


class Connection:
    """One `agy acp` process: JSON-RPC over its stdin and stdout.

    Notifications for a session go to whichever queue a turn installed;
    permission requests are allowed once each, as pcode runs its own tools
    without asking.
    """

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.process: asyncio.subprocess.Process | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.updates: asyncio.Queue | None = None
        self.next_id = 0
        self.reader: asyncio.Task | None = None
        self.stderr: list[str] = []
        # Where updates go while no turn is running: Antigravity keeps working
        # after a turn ends (a background sub-agent's report, its follow-up).
        self.idle: Callable[[dict], None] | None = None

    @property
    def alive(self) -> bool:
        return (
            self.process is not None
            and self.process.returncode is None
            and self.reader is not None
            and not self.reader.done()
        )

    async def start(self) -> dict:
        from importlib.metadata import PackageNotFoundError, version

        try:
            pcode_version = version("pcode")
        except PackageNotFoundError:
            pcode_version = "0"
        self.process = await asyncio.create_subprocess_exec(
            COMMAND,
            "acp",
            # pcode approves every request anyway (see `_answer`), and agy
            # 1.5.13 never forwards a background sub-agent's permission
            # request, which then waits about a minute and fails.
            "--dangerously-skip-permissions",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            # Never onto the terminal UI; kept only to explain a failed start.
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            env=child_env(),
            # One JSON-RPC message per line, and a session/new reply is long.
            limit=16 * 1024 * 1024,
        )
        self.reader = _spawn(self._read())
        _spawn(self._drain_stderr())
        result = await self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                # No client file or terminal access: Antigravity edits files
                # itself and reports diffs, as its own CLI does.
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {"name": "pcode", "title": "pcode", "version": pcode_version},
            },
        )
        if result.get("protocolVersion") != PROTOCOL_VERSION:
            raise AntigravityError(
                f"Antigravity speaks ACP version {result.get('protocolVersion')!r}; "
                f"pcode needs {PROTOCOL_VERSION}. Update the Antigravity CLI."
            )
        return result

    async def _drain_stderr(self) -> None:
        assert self.process and self.process.stderr
        while line := await self.process.stderr.readline():
            self.stderr = [*self.stderr[-19:], line.decode(errors="replace").rstrip()]

    async def _read(self) -> None:
        assert self.process and self.process.stdout
        stopped = False
        try:
            while line := await self.process.stdout.readline():
                try:
                    message = json.loads(line)
                    if isinstance(message, dict):
                        await self._dispatch(message)
                except ValueError, TypeError, KeyError, AttributeError:
                    continue  # One malformed message must not stop the conversation.
        except asyncio.CancelledError:
            stopped = True  # By `close()`, which ends the process itself.
            raise
        finally:
            gone = AntigravityError(self._exit_reason())
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(gone)
            self.pending.clear()
            if self.updates is not None:
                self.updates.put_nowait(gone)
            # A reader that failed leaves nobody to answer the process, so it
            # must not outlive it.
            if not stopped and self.process is not None and self.process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    self.process.kill()

    def _exit_reason(self) -> str:
        detail = next((line for line in reversed(self.stderr) if line.strip()), "")
        return "Antigravity (agy acp) exited" + (f": {detail}" if detail else ".")

    async def _dispatch(self, message: dict) -> None:
        method = message.get("method")
        if method is None:
            future = self.pending.pop(message.get("id"), None)
            if future is not None and not future.done():
                if "error" in message:
                    future.set_exception(rpc_error(message["error"]))
                else:
                    future.set_result(message.get("result") or {})
                if self.updates is not None:
                    # In line, before any update read after it: agy writes a
                    # prompt's reply and what it says after the turn back to
                    # back, and a done-callback would run after both.
                    self.updates.put_nowait(future)
        elif method == "session/update":
            update = (message.get("params") or {}).get("update") or {}
            if self.updates is not None:
                self.updates.put_nowait(update)
            elif self.idle is not None:
                self.idle(update)
        elif "id" in message:
            with contextlib.suppress(AntigravityError, OSError):
                await self._answer(message)

    async def _answer(self, message: dict) -> None:
        if message["method"] == "session/request_permission":
            options = (message.get("params") or {}).get("options") or []
            # Once, never "always": that would write a lasting rule into the
            # user's Antigravity settings.
            chosen = next((o for o in options if o.get("kind") == "allow_once"), None)
            refused = next((o for o in options if o.get("kind") == "reject_once"), None)
            if chosen or refused:
                outcome = {"outcome": "selected", "optionId": (chosen or refused)["optionId"]}
            else:
                outcome = {"outcome": "cancelled"}
            await self._send(
                {"jsonrpc": "2.0", "id": message["id"], "result": {"outcome": outcome}}
            )
            return
        error = {"code": -32601, "message": f"pcode does not support {message['method']}"}
        await self._send({"jsonrpc": "2.0", "id": message["id"], "error": error})

    async def _send(self, message: dict) -> None:
        if not self.alive:
            raise AntigravityError(self._exit_reason())
        assert self.process and self.process.stdin
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        await self.process.stdin.drain()

    def begin(self, method: str, params: dict) -> asyncio.Future:
        """Send a request now; its reply resolves the returned future."""
        self.next_id += 1
        request_id = self.next_id
        future = asyncio.get_running_loop().create_future()
        # A caller that was cancelled never reads the reply; don't let a
        # failure nobody awaits be logged as unretrieved.
        future.add_done_callback(lambda f: f.cancelled() or f.exception())
        self.pending[request_id] = future

        async def send() -> None:
            try:
                await self._send(
                    {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
                )
            except (AntigravityError, OSError) as error:
                self.pending.pop(request_id, None)
                if not future.done():
                    future.set_exception(
                        error
                        if isinstance(error, AntigravityError)
                        else AntigravityError(self._exit_reason())
                    )

        _spawn(send())
        return future

    async def request(self, method: str, params: dict) -> dict:
        return await self.begin(method, params)

    async def notify(self, method: str, params: dict) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def close(self) -> None:
        process, self.process = self.process, None
        if process is not None and process.returncode is None:
            if process.stdin:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        if self.reader:
            self.reader.cancel()


def rpc_error(error: dict) -> AntigravityError:
    text = str(error.get("message") or "request failed")
    data = error.get("data")
    if isinstance(data, (str, dict)) and data:
        text += f": {data if isinstance(data, str) else json.dumps(data)[:300]}"
    if error.get("code") == AUTH_REQUIRED:
        text += f". {LOGIN_HINT}"
    return AntigravityError(f"Antigravity: {text}")


# Every live connection, so pcode's exit can stop them, and the tasks running
# beside a turn, which would otherwise be garbage-collected mid-flight.
_connections: set[Connection] = set()
_background: set[asyncio.Task] = set()


async def shutdown() -> None:
    # Processes first, each closing its stdin and waiting, so `agy` can save
    # the session `--continue` resumes; cancelling their readers first would
    # leave nothing to answer them.
    connections = list(_connections)
    _connections.clear()
    await asyncio.gather(*(c.close() for c in connections), return_exceptions=True)
    from pcode.antigravity_tools import close_all

    await close_all()
    for task in list(_background):
        task.cancel()


async def _close(connection: Connection) -> None:
    _connections.discard(connection)
    await connection.close()


@dataclass
class _Session:
    connection: Connection
    session_id: str
    turns: int = 0
    # Prompts of the newest turns that ended without a reply pcode kept
    # (cancelled, or failed partway): pcode keeps such a request but not its
    # partial response, so the history is that many stamps behind.
    interrupted: list[str] = field(default_factory=list)


def _stamp(session_id: str, turn: int) -> str:
    return f"{session_id}#{turn}"


def _parse_stamp(response: ModelResponse) -> tuple[str, int] | None:
    if response.provider_name != SYSTEM or not response.provider_response_id:
        return None
    session_id, _, turn = response.provider_response_id.partition("#")
    return (session_id, int(turn)) if session_id and turn.isdigit() else None


class AntigravityModel(Model):
    """See the module docstring. One instance serves one conversation."""

    def __init__(self, model_name: str, *, side: bool = False) -> None:
        super().__init__()
        self._model_name = model_name
        self._side = side
        self._session: _Session | None = None
        self._lock = asyncio.Lock()
        # Shows what Antigravity says between turns; set by the runtime.
        self.listener: Callable[[str], None] | None = None
        # Reports titles Antigravity assigns its session (`session_info_update`).
        self.title_listener: Callable[[str], None] | None = None
        self._after_turn: list[str] = []
        self._quiet: asyncio.TimerHandle | None = None
        from pcode.antigravity_tools import ToolBridge

        # pcode's delegation, which Antigravity may call (see antigravity_tools).
        # Side questions never get delegation tools.
        self.tools = None if side else ToolBridge(notify=self._show)

    def _title_update(self, update: dict) -> None:
        title = update.get("title")
        if isinstance(title, str) and title.strip() and self.title_listener is not None:
            with contextlib.suppress(Exception):  # A display failure must not stop agy.
                self.title_listener(title)

    def _usage_update(self, update: dict) -> None:
        from pcode.model_metadata import positive_int, record_context_window

        size = positive_int(update.get("size"))
        if size is not None:
            record_context_window(self, size, source=SYSTEM)
            if self._model_name:
                _remember_window(self._model_name, size)

    def _idle_update(self, update: dict) -> None:
        """Show output Antigravity produces on its own between turns; display only.

        pcode never answers it with a prompt: that would be pcode driving
        Antigravity. Text is gathered until it goes quiet, and each tool it
        finishes is a line of its own.
        """
        kind = update.get("sessionUpdate")
        content = update.get("content")
        if kind == "usage_update":
            self._usage_update(update)
        elif kind == "session_info_update":
            self._title_update(update)
        elif kind == "agent_message_chunk" and isinstance(content, dict):
            self._after_turn.append(str(content.get("text", "")))
            if self._quiet is not None:
                self._quiet.cancel()
            self._quiet = asyncio.get_running_loop().call_later(IDLE_QUIET, self._flush_idle)
        elif kind == "tool_call_update" and update.get("status") in {"completed", "failed"}:
            self._flush_idle()
            title = update.get("title") or update.get("name") or "a tool"
            failed = " (failed)" if update.get("status") == "failed" else ""
            self._show(f"Antigravity, after the turn, ran {title}{failed}")

    def _flush_idle(self) -> None:
        if self._quiet is not None:
            self._quiet.cancel()
            self._quiet = None
        text, self._after_turn = "".join(self._after_turn).strip(), []
        if text:
            self._show(f"Antigravity, after the turn:\n{text}")

    def _show(self, text: str) -> None:
        if self.listener is not None:
            with contextlib.suppress(Exception):  # A display failure must not stop agy.
                self.listener(text)

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def system(self) -> str:
        return SYSTEM

    def retire(self) -> None:
        """Stop this model's process once any turn on it ends; for a replaced agent."""
        with contextlib.suppress(RuntimeError):  # No running loop: nothing was started.

            async def retire() -> None:
                async with self._lock:
                    await self._drop()
                    if self.tools is not None:
                        await self.tools.close()

            # What this conversation's agy said after its last turn must not
            # land in whichever conversation the view shows next.
            self.listener = None
            self.title_listener = None
            if self._quiet is not None:
                self._quiet.cancel()
                self._quiet = None
            self._after_turn = []

            _spawn(retire())

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        async with self.request_stream(
            messages, model_settings, model_request_parameters
        ) as stream:
            async for _ in stream:
                pass
        return stream.get()

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncIterator[StreamedResponse]:
        from pcode.claude_sdk.workspace import CWD_SETTING

        # Before the lock or a process: a refusal must cost nothing.
        prompt, interrupted = user_prompt(messages, side=self._side)
        # Anything said since the last turn shows before this one starts.
        self._flush_idle()
        earlier = messages[:-1]
        if interrupted:
            earlier = [*earlier, ModelRequest(parts=interrupted)]
        cwd = str((model_settings or {}).get(CWD_SETTING) or os.getcwd())
        await self._lock.acquire()
        handed_off = False
        # Set first: a new session lists the tools as this turn has them.
        if self.tools is not None:
            self.tools.begin_turn(run_context)
        try:
            session, replay = await self._prepare(earlier, _asked(interrupted), cwd)
            stream = AntigravityStream(
                model_request_parameters=model_request_parameters,
                model_id=self._model_name,
                session=session,
                prompt=[*replay, *prompt],
                replayed=len(replay),
                run_context=run_context,
                notify=self._show,
                on_usage=self._usage_update,
                on_title=self._title_update,
            )
            try:
                yield stream
            finally:
                if stream.unfinished or (stream.failed and stream.events_seen):
                    # As pcode keeps the request: the prompt, then any steering.
                    session.interrupted.append("\n".join(t for t in stream.asked if t))
                if stream.unfinished:
                    # Stopped mid-turn. The run's cancellation is level-triggered,
                    # so waiting for Antigravity to confirm here would itself be
                    # cancelled; a task does it, keeping the lock so the next
                    # prompt waits for the turn to really stop.
                    _spawn(self._settle(stream))
                    handed_off = True
                elif stream.broken:
                    await asyncio.shield(self._drop())
        finally:
            if self.tools is not None:
                self.tools.cancel_calls()
            if not handed_off:
                try:
                    # The next prompt waits for a cancelled worker's cleanup.
                    if self.tools is not None:
                        await asyncio.shield(self.tools.drained())
                finally:
                    self._lock.release()

    async def _settle(self, stream: AntigravityStream) -> None:
        try:
            if not await stream.stop():
                await self._drop()
        except Exception:
            await self._drop()
        finally:
            try:
                if self.tools is not None:
                    await self.tools.drained()
            finally:
                self._lock.release()

    async def _drop(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            await _close(session.connection)

    async def _prepare(
        self, earlier: list[ModelMessage], interrupted: list[str], cwd: str
    ) -> tuple[_Session, list[dict]]:
        """The session to prompt, and any replay to send before the new prompt.

        `interrupted` holds the prompts of unanswered requests ending `earlier`.
        """
        last = next((m for m in reversed(earlier) if isinstance(m, ModelResponse)), None)
        stamp = _parse_stamp(last) if last is not None else None
        session = self._session
        if session is not None:
            current = (session.session_id, session.turns)
            # The session's extra turns are the interrupted requests this
            # history kept (with no reply before them when they were its first).
            ahead = len(session.interrupted)
            behind = session.turns - ahead
            same = stamp == current or (
                ahead > 0
                and interrupted == session.interrupted
                and stamp == ((session.session_id, behind) if behind > 0 else None)
            )
            # Another process (a fork resumed elsewhere) may have moved it on.
            # No entry at all means the ledger can't be kept, not that it moved.
            ours = newest_turn(session.session_id) in (None, session.turns)
            if session.connection.alive and session.connection.cwd == cwd and same and ours:
                if stamp == current:
                    # A kept reply: nothing after it is unanswered any more.
                    session.interrupted = []
                return session, []
            await self._drop()
        resumable = (
            not self._side
            and stamp is not None
            and _all_from(earlier, stamp[0])
            and newest_turn(stamp[0]) == stamp[1]
        )
        connection = Connection(cwd)
        _connections.add(connection)
        try:
            await connection.start()
            servers = await self.tools.servers() if self.tools is not None else []
            result = None
            if resumable:
                assert stamp is not None
                with contextlib.suppress(AntigravityError):
                    result = await connection.request(
                        "session/resume",
                        {"sessionId": stamp[0], "cwd": cwd, "mcpServers": servers},
                    )
                    result = {**result, "sessionId": stamp[0]}
            replay = result is None
            if result is None:
                result = await connection.request(
                    "session/new", {"cwd": cwd, "mcpServers": servers}
                )
            _remember_models(result)
            session_id = result["sessionId"]
            current_model = (result.get("models") or {}).get("currentModelId") or ""
            if self._model_name and self._model_name != current_model:
                try:
                    await connection.request(
                        "session/set_model",
                        {"sessionId": session_id, "modelId": self._model_name},
                    )
                except AntigravityError as error:
                    offered = _offered(result) or list(cached_models())
                    raise AntigravityError(
                        f"{error} Models this account offers: {', '.join(offered)}"
                        if offered
                        else str(error)
                    ) from error
        except BaseException:
            # Cancelled here, an await would be cancelled too; close beside it.
            _spawn(_close(connection))
            raise
        # Attach only once the session is on `self._model_name`, so an early
        # `usage_update` for `session/new`'s default model isn't cached under it.
        connection.idle = self._idle_update
        turns = stamp[1] if not replay and stamp is not None else 0
        self._session = _Session(connection, session_id, turns)
        intro = SIDE_REPLAY_INTRO if self._side else REPLAY_INTRO
        blocks = (
            [{"type": "text", "text": replay_text(earlier, intro=intro)}]
            if replay and earlier
            else []
        )
        return self._session, blocks


def _all_from(earlier: list[ModelMessage], session_id: str) -> bool:
    """Whether every response in `earlier` came from this one Antigravity session."""
    responses = [m for m in earlier if isinstance(m, ModelResponse)]
    return bool(responses) and all((_parse_stamp(m) or ("",))[0] == session_id for m in responses)


def _markers() -> tuple[str, ...]:
    from pcode.live import CANCELLED_MARKER, UNANSWERED_MARKER

    return (CANCELLED_MARKER, UNANSWERED_MARKER)


def _asked(parts: list) -> list[str]:
    """Each unanswered prompt's text in a merged request, to compare with what was sent.

    Every one ends in one of pcode's markers.
    """
    asked: list[str] = []
    texts: list[str] = []
    for part in parts:
        if not isinstance(part, UserPromptPart):
            continue
        if part.content in _markers():
            asked.append("\n".join(texts))
            texts = []
            continue
        items = [part.content] if isinstance(part.content, str) else part.content
        texts += [item for item in items if isinstance(item, str) and item.strip()]
    return asked


def _asked_blocks(blocks: list[dict]) -> str:
    return "\n".join(b["text"] for b in blocks if b.get("type") == "text")


def user_prompt(messages: list[ModelMessage], *, side: bool = False) -> tuple[list[dict], list]:
    """The user's new prompt as ACP content blocks; refuses anything else.

    Only a request carrying the user's own message may reach Antigravity.
    Tool results (a turn started on another model), retry prompts, and a
    continuation with no new message would be pcode prompting it by itself.

    A request a cancelled or failed turn left unanswered ends in one of pcode's
    markers (see `live.mark_unanswered`), and Pydantic AI merges the next prompt
    into it. Its parts up to the marker are returned second: they belong to
    the history, not the new prompt.

    For a `/btw` side question (`side=True`), a mid-turn request may hold the
    in-flight turn's prompt or tool results ahead of the side question's own
    `UserPromptPart`; those earlier parts are returned second so they join the
    replayed context rather than the side prompt.
    """
    request = messages[-1] if messages else None
    if not isinstance(request, ModelRequest):
        raise AntigravityError(OWN_PROMPTS_ONLY)
    markers = _markers()
    marker = max(
        (
            i
            for i, part in enumerate(request.parts)
            if isinstance(part, UserPromptPart) and part.content in markers
        ),
        default=-1,
    )
    if side:
        last_user = max(
            (
                i
                for i in range(marker + 1, len(request.parts))
                if isinstance(request.parts[i], UserPromptPart)
            ),
            default=-1,
        )
        if last_user < 0:
            raise AntigravityError(OWN_PROMPTS_ONLY)
        interrupted, parts = list(request.parts[:last_user]), request.parts[last_user:]
    else:
        interrupted, parts = list(request.parts[: marker + 1]), request.parts[marker + 1 :]
    if any(isinstance(part, (ToolReturnPart, RetryPromptPart)) for part in parts):
        raise AntigravityError(OWN_PROMPTS_ONLY)
    blocks: list[dict] = []
    unsupported: list[str] = []
    for part in parts:
        if isinstance(part, UserPromptPart):
            more, refused = content_blocks(part.content)
            blocks += more
            unsupported += refused
    if unsupported:
        raise AntigravityError(
            f"Antigravity can take text and images here, not {', '.join(sorted(set(unsupported)))}."
        )
    if not blocks:
        raise AntigravityError(OWN_PROMPTS_ONLY)
    return blocks, interrupted


def content_blocks(content) -> tuple[list[dict], list[str]]:
    """User content as ACP blocks, and the type names of any it can't carry."""
    blocks: list[dict] = []
    unsupported: list[str] = []
    for item in [content] if isinstance(content, str) else list(content):
        if isinstance(item, str):
            if item.strip():
                blocks.append({"type": "text", "text": item})
        elif isinstance(item, BinaryContent) and item.is_image:
            data = base64.b64encode(item.data).decode()
            blocks.append({"type": "image", "mimeType": item.media_type, "data": data})
        else:
            unsupported.append(type(item).__name__)
    return blocks, unsupported


def _clip(text: str, limit: int = 2000) -> str:
    return text if len(text) <= limit else text[:limit] + " …"


def replay_text(messages: list[ModelMessage], *, intro: str = REPLAY_INTRO) -> str:
    """Earlier turns as a transcript, for a session that never saw them."""
    lines = [intro, "", "<conversation>"]
    for message in messages:
        for part in message.parts:
            if isinstance(part, UserPromptPart):
                content = (
                    part.content
                    if isinstance(part.content, str)
                    else " ".join(item for item in part.content if isinstance(item, str))
                )
                if content.strip():
                    lines += [f"[User]\n{content}", ""]
            elif isinstance(part, TextPart) and part.content.strip():
                lines += [f"[Assistant]\n{part.content}", ""]
            elif isinstance(part, (NativeToolCallPart, ToolCallPart)):
                args = _clip(json.dumps(part.args, default=str), 500)
                lines += [f"[Assistant ran {part.tool_name}: {args}]", ""]
            elif isinstance(part, (NativeToolReturnPart, ToolReturnPart)):
                lines += [f"[{part.tool_name} returned: {_clip(part.model_response_str())}]", ""]
    lines.append("</conversation>")
    return "\n".join(lines)


def _tool_args(update: dict) -> dict:
    raw = update.get("rawInput")
    args = dict(raw) if isinstance(raw, dict) else {}
    # Antigravity capitalizes argument names (`Path`, `CommandLine`); pcode's
    # tool rows read lower-case ones.
    args = {k[:1].lower() + k[1:]: v for k, v in args.items()}
    if "commandLine" in args and "command" not in args:
        args["command"] = args["commandLine"]
    if update.get("title") and "title" not in args:
        args["title"] = update["title"]
    return args


def _tool_output(update: dict) -> object:
    texts = []
    for block in update.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "diff":
            texts.append(f"Edited {block.get('path', '')}")
        elif isinstance(inner := block.get("content"), dict) and inner.get("type") == "text":
            texts.append(str(inner.get("text", "")))
    if texts:
        return "\n".join(texts)
    raw = update.get("rawOutput")
    return raw if raw not in (None, {}) else ""


@dataclass
class _ToolState:
    """Tracked metadata for a tool call in the current turn."""

    kind: str = ""
    title: str = ""
    is_daemon: bool = False
    status: str = ""

    @property
    def outlives_prompt(self) -> bool:
        """Whether `agy` keeps running this tool after `session/prompt` settles."""
        if self.status != "in_progress":
            return False
        if self.title in SCHEDULE_TITLES:
            return True
        return self.kind == "execute" and not self.is_daemon


@dataclass
class AntigravityStream(StreamedResponse):
    """One Antigravity turn as Pydantic AI stream events."""

    model_id: str = ""
    session: _Session | None = None
    prompt: list[dict] = field(default_factory=list)
    # How many of `prompt`'s blocks replay earlier history.
    replayed: int = 0
    # The run, for the user's steering messages; None outside an agent run.
    run_context: RunContext[Any] | None = None
    # Tells the user something about the turn (the model's `_show`).
    notify: Callable[[str], None] = lambda text: None
    # Records context window sizes reported by ACP `usage_update` notifications.
    on_usage: Callable[[dict], None] = lambda update: None
    # Reports session titles emitted by ACP `session_info_update` notifications.
    on_title: Callable[[dict], None] = lambda update: None
    started: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # The newest prompt's reply. A steering message is a further prompt, and
    # Antigravity ends the one before it as soon as it arrives.
    reply: asyncio.Future | None = field(default=None, init=False)
    replies: list[asyncio.Future] = field(default_factory=list, init=False)
    # What this turn has told Antigravity: the prompt, then each steering message.
    asked: list[str] = field(default_factory=list, init=False)
    broken: bool = field(default=False, init=False)
    # True while `session/prompt` has settled but we are still holding the turn
    # open for a running command/schedule and its unsolicited follow-up reply.
    holding: bool = field(default=False, init=False)
    part_count: int = field(default=0, init=False)
    part_kind: str = field(default="", init=False)
    calls: dict[str, tuple[str, dict]] = field(default_factory=dict, init=False)
    tools: dict[str, _ToolState] = field(default_factory=dict, init=False)
    text_seen: bool = field(default=False, init=False)
    events_seen: bool = field(default=False, init=False)
    failed: bool = field(default=False, init=False)
    context_used: int | None = field(default=None, init=False)

    @property
    def model_name(self) -> str:
        return self.model_id

    @property
    def provider_name(self) -> str:
        return SYSTEM

    @property
    def provider_url(self) -> str | None:
        return None

    @property
    def timestamp(self) -> datetime:
        return self.started

    @property
    def unfinished(self) -> bool:
        return (self.reply is not None and not self.reply.done()) or self.holding

    def _has_outliving_tools(self) -> bool:
        return any(tool.outlives_prompt for tool in self.tools.values())

    def _should_hold(self, reply: asyncio.Future) -> bool:
        if reply.cancelled() or reply.exception() is not None:
            return False
        if reply.result().get("stopReason") == "cancelled":
            return False
        return self._has_outliving_tools()

    def _vendor(self, kind: str) -> str:
        """A part ID that starts a new part whenever the kind of output changes."""
        if kind != self.part_kind:
            self.part_kind = kind
            self.part_count += 1
        return f"{kind}-{self.part_count}"

    async def _get_event_iterator(self) -> AsyncIterator[ModelResponseStreamEvent]:
        assert self.session is not None
        session = self.session
        connection = session.connection
        # Stamped up front so an interrupted reply, which pcode keeps, still
        # names the session that holds the rest of it.
        session.turns += 1
        self.provider_response_id = _stamp(session.session_id, session.turns)
        _record_turn(session.session_id, session.turns)
        updates: asyncio.Queue = asyncio.Queue()
        connection.updates = updates
        # What the user asked, not the replay sent ahead of it.
        self._send_prompt(self.prompt, updates, asked=_asked_blocks(self.prompt[self.replayed :]))
        try:
            async for event in self._events(updates, session):
                yield event
        except Exception:
            self.failed = True
            raise
        finally:
            if connection.updates is updates:
                connection.updates = None
                # What came after the turn's end belongs to the time between turns.
                while not updates.empty():
                    leftover = updates.get_nowait()
                    if isinstance(leftover, dict) and connection.idle is not None:
                        connection.idle(leftover)
        if not self.text_seen:
            # A reply with no text reads as empty, and Pydantic AI would ask
            # again: that request would be pcode's, not the user's.
            yield self._parts_manager.handle_part(
                vendor_part_id="text-end", part=TextPart("(Antigravity finished without a reply.)")
            )

    async def _events(
        self, updates: asyncio.Queue, session: _Session
    ) -> AsyncIterator[ModelResponseStreamEvent]:
        loop = asyncio.get_running_loop()
        reply_open = False
        quiet_deadline: float | None = None
        while True:
            if self.run_context is not None and steering_pending(self.run_context):
                previous = self.reply
                await self._steer(updates)
                if self.reply is not previous:
                    self.holding = False
                    reply_open = False
                    quiet_deadline = None
            timeout = STEER_POLL
            if self.holding and quiet_deadline is not None:
                remaining = quiet_deadline - loop.time()
                if remaining <= 0:
                    self.holding = False
                    break
                timeout = min(timeout, remaining)
            try:
                update = await asyncio.wait_for(updates.get(), timeout)
            except TimeoutError:
                if self.holding and quiet_deadline is not None and loop.time() >= quiet_deadline:
                    self.holding = False
                    break
                continue
            if isinstance(update, BaseException):
                self.holding = False
                self.broken = True
                raise update
            if isinstance(update, asyncio.Future):
                if update is self.reply and not self.holding:
                    if not self._should_hold(update):
                        self.holding = False
                        break
                    self.holding = True
                    reply_open = False
                    quiet_deadline = None
                continue  # A prompt a steering message took over, or held for background work.
            kind = update.get("sessionUpdate")
            for event in self._map(update):
                self.events_seen = True
                yield event
            if self.holding:
                if kind == "usage_update":
                    reply_open = False
                elif kind in {
                    "agent_message_chunk",
                    "agent_thought_chunk",
                    "tool_call",
                    "tool_call_update",
                }:
                    reply_open = True
                if self._has_outliving_tools():
                    quiet_deadline = None
                else:
                    limit = OPEN_REPLY_QUIET_LIMIT if reply_open else DEFERRED_FINALIZE_DEBOUNCE
                    quiet_deadline = loop.time() + limit
        assert self.reply is not None
        try:
            result = self.reply.result()
        except AntigravityError:
            if not self.events_seen and len(self.replies) == 1:
                # Refused before it started (signed out, unknown model): the
                # session never saw this turn.
                session.turns -= 1
                _record_turn(session.session_id, session.turns)
            raise
        self._finish(result)

    def _send_prompt(
        self, blocks: list[dict], updates: asyncio.Queue, *, asked: str | None = None
    ) -> None:
        assert self.session is not None
        reply = self.session.connection.begin(
            "session/prompt", {"sessionId": self.session.session_id, "prompt": blocks}
        )
        # The future itself marks where its prompt's updates end.
        reply.add_done_callback(updates.put_nowait)
        self.reply = reply
        self.replies.append(reply)
        self.asked.append(_asked_blocks(blocks) if asked is None else asked)

    async def _steer(self, updates: asyncio.Queue) -> None:
        """Send the user's steering messages into the running turn, as Antigravity's own clients do.

        Each is a further `session/prompt`; Antigravity folds it into the work
        in progress. Only the messages go: never pcode's steering reminder.
        They are added to this turn's request as well, as pcode's other
        models get them, so the history says what Antigravity was told.
        """
        from pcode.retries import RequestCheckpoint
        from pcode.steering import take_user_steering

        ctx = self.run_context
        assert ctx is not None
        # The run's request, and the copy pcode restores if this turn is
        # cancelled or fails: both must say what Antigravity was told.
        requests = [ctx.messages[-1]] if ctx.messages else []
        for capability in ctx.capabilities.values():
            if isinstance(capability, RequestCheckpoint) and capability.messages:
                requests.append(capability.messages[-1])
        for message in await take_user_steering(ctx):
            blocks, refused = content_blocks(message)  # Only text and images can go.
            if refused:
                self.notify(
                    f"Antigravity can't take {', '.join(sorted(set(refused)))}; "
                    "sent the rest of that message."
                )
            for request in requests:
                if isinstance(request, ModelRequest):
                    request.parts.append(UserPromptPart(message))
            if blocks:
                self._send_prompt(blocks, updates)

    def _finish(self, result: dict) -> None:
        stop = result.get("stopReason")
        reasons = {"end_turn": "stop", "max_tokens": "length", "refusal": "content_filter"}
        self.finish_reason = reasons.get(stop, "stop")
        details: dict[str, Any] = {}
        if stop:
            details["stop_reason"] = stop
        if self.context_used is not None:
            details["context_used"] = self.context_used
        self.provider_details = details or None
        # Each prompt reports its own usage; a steered turn is all of them.
        total = RequestUsage()
        for reply in self.replies:
            if not reply.done() or reply.cancelled() or reply.exception():
                continue
            usage = reply.result().get("usage") or {}
            cached = int(usage.get("cachedReadTokens") or 0)
            written = int(usage.get("cachedWriteTokens") or 0)
            total += RequestUsage(
                input_tokens=int(usage.get("inputTokens") or 0) + cached + written,
                cache_read_tokens=cached,
                cache_write_tokens=written,
                output_tokens=int(usage.get("outputTokens") or 0),
            )
        if not total.input_tokens and self.context_used:
            total += RequestUsage(input_tokens=self.context_used)
        self._usage = total

    def _map(self, update: dict) -> Iterator[ModelResponseStreamEvent]:
        kind = update.get("sessionUpdate")
        content = update.get("content")
        text = content.get("text", "") if isinstance(content, dict) else ""
        if kind == "usage_update":
            self.on_usage(update)
            used = update.get("used")
            if type(used) is int and used >= 0:
                self.context_used = used
        elif kind == "session_info_update":
            self.on_title(update)
        elif kind == "agent_message_chunk" and text:
            self.text_seen = self.text_seen or bool(text.strip())
            yield from self._parts_manager.handle_text_delta(
                vendor_part_id=self._vendor("text"), content=text
            )
        elif kind == "agent_thought_chunk" and text:
            yield from self._parts_manager.handle_thinking_delta(
                vendor_part_id=self._vendor("thinking"), content=text
            )
        elif kind in {"tool_call", "tool_call_update"}:
            yield from self._tool(update)

    def _tool(self, update: dict) -> Iterator[ModelResponseStreamEvent]:
        call_id = str(update.get("toolCallId") or "")
        if not call_id:
            return
        state = self.tools.setdefault(call_id, _ToolState())
        if isinstance(update.get("kind"), str) and update["kind"]:
            state.kind = update["kind"]
        if isinstance(update.get("title"), str) and update["title"]:
            state.title = update["title"]
        raw_input = update.get("rawInput")
        if isinstance(raw_input, dict) and (
            raw_input.get("IsDaemon") is True or raw_input.get("isDaemon") is True
        ):
            state.is_daemon = True
        status = update.get("status")
        if isinstance(status, str) and status:
            state.status = status
        known = self.calls.get(call_id)
        args = _tool_args(update)
        # The first report may carry no arguments yet; a later one fills them in.
        if known is None or (not known[1] and update.get("rawInput")):
            name = known[0] if known else str(update.get("name") or update.get("kind") or "tool")
            self.calls[call_id] = (name, args)
            if known is None:
                self._vendor("tool")
            yield self._parts_manager.handle_part(
                vendor_part_id=f"call-{call_id}",
                part=NativeToolCallPart(
                    tool_name=name, args=args, tool_call_id=call_id, provider_name=SYSTEM
                ),
            )
        if status in {"completed", "failed"}:
            name, _ = self.calls[call_id]
            yield self._parts_manager.handle_part(
                vendor_part_id=f"return-{call_id}",
                part=NativeToolReturnPart(
                    tool_name=name,
                    content=_tool_output(update),
                    tool_call_id=call_id,
                    provider_name=SYSTEM,
                    outcome="success" if status == "completed" else "failed",
                ),
            )

    async def close_stream(self) -> None:
        await self._cancel()

    async def _cancel(self) -> None:
        if self.session is None or not self.unfinished:
            return
        with contextlib.suppress(AntigravityError, OSError):
            await self.session.connection.notify(
                "session/cancel", {"sessionId": self.session.session_id}
            )

    async def stop(self) -> bool:
        """Cancel an unfinished turn and wait for it to end; whether it did.

        A turn left running would keep spending the subscription's quota, and the
        next prompt would steer it instead of starting a new one.
        """
        await self._cancel()
        assert self.reply is not None
        try:
            await asyncio.wait_for(asyncio.shield(self.reply), CANCEL_GRACE)
        except Exception:
            return False
        return True


def antigravity_model(model: str, *, side: bool = False) -> AntigravityModel:
    name = model.removeprefix(PREFIX).strip()
    if not installed():
        raise ValueError(
            "Antigravity CLI (agy) is not installed: https://antigravity.google/docs/cli/"
        )
    return AntigravityModel(name, side=side)
