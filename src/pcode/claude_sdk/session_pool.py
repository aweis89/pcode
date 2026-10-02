"""Live CLI processes for one event loop, matched to requests by history."""

import asyncio
import logging
import time
import weakref
from contextlib import suppress
from dataclasses import dataclass

from pcode.claude_sdk.errors import ClaudeProcessError, ClaudeStartError
from pcode.claude_sdk.messages import _blocks, _result_ids, _tool_use_ids, replay
from pcode.claude_sdk.resume import ForkPoint, ResumeIndex
from pcode.claude_sdk.session import ClaudeSession, SessionConfig, _Diverged

logger = logging.getLogger(__name__)

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
