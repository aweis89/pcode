"""Inspect, query, and coordinate other pcode session hosts.

Lets a session act as a conductor over other open `/attach` hosts and saved
historical sessions:
- `list_hosts` / `inspect_host` (read-only, also available to workers) for
  discovering open/stopped hosts and reading live plan, job, and busy state.
- `ask_host` (read-only headless `/btw`) for asking a session a question
  against its full context without mutating its main thread.
- `send_to_host` / `wait_for_hosts` / `stop_host` for actively driving or
  closing other sessions.

`ask_host` and `send_to_host` accept either a running host ID or a saved
session ID, resuming a stopped or historical session in its original workspace
automatically. Disabled inside unattended remote-profile hosts.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path
from typing import Literal

from pydantic_ai import ModelRetry, RunContext, Tool
from pydantic_ai.capabilities import Capability

from pcode import remote, remote_profile
from pcode.aside import ASIDE_TIMEOUT_SECONDS
from pcode.conversation_ids import base_conversation
from pcode.host_protocol import HostEntry, host_dir, list_stopped
from pcode.host_protocol import list_hosts as discover_hosts
from pcode.remote import HostError
from pcode.rpc import RemoteError

PARENT_INSTRUCTIONS = (
    "You can inspect, query, and manage other pcode sessions. "
    "For triaging many open or historical sessions, start with list_hosts (or search_sessions) "
    "and delegate parallel read-only workers using inspect_host and read_session so this "
    "conversation stays concise. Use ask_host for a read-only side question against a session's "
    "full context, send_to_host (with wait=False + wait_for_hosts for parallel fan-out) to send "
    "work, and stop_host to close finished idle sessions."
)

WORKER_INSTRUCTIONS = (
    "Use list_hosts and inspect_host to check live session host state (plan, running jobs, "
    "activity, busy reason) alongside read_session for saved turns."
)

StateFilter = Literal["all", "working", "idle", "stopped"]
WaitMode = Literal["all", "any"]

# Reap hosts spawned by auto-resume so they do not linger as zombies.
_children: set[asyncio.Task] = set()


def _reap(process: subprocess.Popen) -> None:
    task = asyncio.get_running_loop().create_task(asyncio.to_thread(process.wait))
    _children.add(task)
    task.add_done_callback(_children.discard)


def _self_session(ctx: RunContext) -> str:
    return base_conversation(getattr(ctx, "conversation_id", None)) or ""


def _is_self(entry: HostEntry, my_session: str) -> bool:
    return entry.pid == os.getpid() or (bool(my_session) and entry.session_id == my_session)


def _entry_summary(entry: HostEntry, my_session: str) -> dict:
    return {
        "host_id": entry.id,
        "session_id": entry.session_id,
        "workspace": entry.workspace,
        "model": entry.model,
        "state": entry.state,
        "outcome": entry.outcome,
        "turns": entry.turns,
        "unseen": entry.unseen,
        "attached": entry.attached,
        "held": entry.held,
        "title": entry.title,
        "last_prompt": entry.last_prompt,
        "forked_from": entry.forked_from,
        "updated": entry.updated,
        "is_self": _is_self(entry, my_session),
    }


def _match_entries(entries: list[HostEntry], selector: str) -> list[HostEntry]:
    return [
        entry
        for entry in entries
        if entry.id.startswith(selector)
        or (entry.session_id and entry.session_id.startswith(selector))
    ]


def _find_running(selector: str, directory: Path | None = None) -> HostEntry | None:
    matches = _match_entries(discover_hosts(directory), selector)
    if len(matches) > 1:
        ids = ", ".join(f"{e.id} ({e.session_id or 'unsaved'})" for e in matches[:6])
        raise ModelRetry(f"Selector {selector!r} matches multiple running hosts: {ids}.")
    return matches[0] if matches else None


def _older_host_message(entry: HostEntry, action: str) -> str:
    return (
        f"Host {entry.id} is running older pcode that does not support `{action}`; "
        "restart it with `/restart` in its terminal or `pcode --restart-hosts`."
    )


async def _resume_session_host(
    selector: str,
    *,
    session_dir: Path | None,
    my_session: str,
    directory: Path | None = None,
    timeout: float = 60.0,
) -> HostEntry:
    """Start a headless host for a stopped or historical session in its original workspace."""
    from pcode.host_protocol import find_host, socket_path
    from pcode.preferences import load_preferences
    from pcode.sessions import SessionError, is_open, read_info, resolve_session

    stopped_matches = _match_entries(list_stopped(directory), selector)
    if len(stopped_matches) > 1:
        ids = ", ".join(e.session_id or e.id for e in stopped_matches[:6])
        raise ModelRetry(f"Selector {selector!r} matches multiple stopped sessions: {ids}.")
    session_selector = (
        stopped_matches[0].session_id
        if stopped_matches and stopped_matches[0].session_id
        else selector
    )

    try:
        session_path = resolve_session(session_selector, session_dir)
        info = read_info(session_path)
    except (SessionError, OSError, ValueError) as error:
        raise ModelRetry(
            f"No running host or saved session matching {selector!r}: {error}"
        ) from None

    if my_session and info.id == my_session:
        raise ModelRetry("Cannot target the current session.")

    # Re-check running hosts by resolved full session ID in case a short/host prefix missed it.
    if running := _find_running(info.id, directory):
        return running

    if is_open(session_path):
        raise ModelRetry(
            f"Session {info.id} is currently open in an interactive terminal without a host; "
            "cannot resume it from here."
        )

    workspace = Path(info.workspace)
    if not workspace.is_dir():
        raise ModelRetry(
            f"Session {info.id}'s workspace ({workspace}) no longer exists; "
            f"use read_session(session_id={info.id!r}, scope='all') to inspect its saved history."
        )

    model = info.model or load_preferences().get("model")
    if not model:
        raise ModelRetry(f"Session {info.id} has no model recorded and no default model is set.")

    identity, process, log = remote.spawn_host(
        model=model,
        workspace=workspace,
        resume=info.id,
        session_dir=session_dir,
        directory=directory,
        no_worktree=True,
    )
    _reap(process)
    sock = socket_path(identity, directory)
    deadline = time.monotonic() + timeout
    while not sock.exists():
        if process.poll() is not None:
            tail = ""
            try:
                tail = "\n".join(log.read_text(errors="replace").strip().splitlines()[-8:])
            except OSError:
                pass
            raise ModelRetry(
                f"Failed to resume session {info.id} (host exited {process.returncode})."
                + (f"\n{tail}" if tail else "")
            )
        if time.monotonic() > deadline:
            raise ModelRetry(
                f"Session {info.id} host did not start within {timeout:g}s; see {log}."
            )
        await asyncio.sleep(0.05)

    try:
        return find_host(identity, directory)
    except LookupError as error:
        raise ModelRetry(f"Resumed host {identity} disappeared: {error}") from None


async def _ensure_host(
    target: str,
    *,
    session_dir: Path | None,
    my_session: str,
    directory: Path | None = None,
) -> tuple[HostEntry, bool]:
    """Return `(entry, resumed)` for a running host ID or saved session ID."""
    selector = target.strip()
    if not selector:
        raise ModelRetry("Provide a host_id or session_id.")
    if entry := _find_running(selector, directory):
        if _is_self(entry, my_session):
            raise ModelRetry("Cannot target the current session.")
        return entry, False
    entry = await _resume_session_host(
        selector, session_dir=session_dir, my_session=my_session, directory=directory
    )
    return entry, True


def _host_is_busy(snapshot: dict) -> bool:
    activity = snapshot.get("activity") or {}
    return bool(
        snapshot.get("startup_pending")
        or activity.get("busy")
        or (activity.get("queued") or 0) > 0
        or snapshot.get("state") in ("starting", "working")
    )


def setup(pcode) -> None:
    if remote_profile.active() is not None:
        return

    session_dir = pcode.session_dir

    async def list_hosts(
        ctx: RunContext,
        workspace: str = "",
        state: StateFilter = "all",
        include_stopped: bool = True,
    ) -> dict:
        """List open and recently stopped `/attach` session hosts.

        Args:
            workspace: Optional case-insensitive substring filter on workspace path.
            state: Filter by host state: all, working, idle, or stopped.
            include_stopped: Include stopped sessions that still appear in `/attach`.
        """
        my_session = _self_session(ctx)
        directory = host_dir()
        entries = list(discover_hosts(directory))
        if include_stopped and state in ("all", "stopped"):
            entries.extend(list_stopped(directory))
        needle = workspace.strip().lower()
        items = []
        for entry in entries:
            if state != "all" and entry.state != state:
                continue
            if needle and needle not in entry.workspace.lower():
                continue
            items.append(_entry_summary(entry, my_session))
        return {"count": len(items), "hosts": items}

    async def inspect_host(ctx: RunContext, host_id: str) -> dict:
        """Read a live host's in-memory state (plan, running jobs, asides, activity).

        Args:
            host_id: Running host ID or session ID prefix.
        """
        selector = host_id.strip()
        if not selector:
            raise ModelRetry("Provide a host_id or session_id prefix.")
        my_session = _self_session(ctx)
        directory = host_dir()
        entry = _find_running(selector, directory)
        if entry is None:
            stopped = _match_entries(list_stopped(directory), selector)
            if stopped:
                sid = stopped[0].session_id or stopped[0].id
                return {
                    "host_id": stopped[0].id,
                    "session_id": sid,
                    "state": "stopped",
                    "workspace": stopped[0].workspace,
                    "title": stopped[0].title,
                    "outcome": stopped[0].outcome,
                    "turns": stopped[0].turns,
                    "note": (
                        "This session's host is stopped. Use read_session to inspect its saved "
                        "history, or ask_host / send_to_host to resume it."
                    ),
                }
            raise ModelRetry(
                f"No running host matching {selector!r}. Use list_hosts to see open hosts, "
                "or search_sessions / read_session for historical sessions."
            )
        try:
            snapshot = await remote.request_host(entry, "peek", timeout=10.0)
        except RemoteError as error:
            if error.type_name == "PermissionError":
                raise ModelRetry(_older_host_message(entry, "peek")) from None
            raise ModelRetry(f"Host {entry.id} error: {error}") from None
        except (HostError, OSError) as error:
            raise ModelRetry(f"Could not inspect host {entry.id}: {error}") from None
        snapshot["is_self"] = _is_self(entry, my_session)
        return snapshot

    async def ask_host(
        ctx: RunContext,
        target: str,
        question: str,
        model: str = "",
    ) -> dict:
        """Ask a session a read-only side question (`/btw`) against its full context.

        Does not append to the target's main conversation or wait behind an active turn.
        Accepts a running host ID or a saved session ID (auto-resuming in its workspace).

        Args:
            target: Running host ID or saved session ID (or unique prefix).
            question: The question to ask the session.
            model: Optional model override (e.g. `anthropic:claude-sonnet-4-6+low`).
        """
        cleaned = question.strip()
        if not cleaned:
            raise ModelRetry("Provide a non-empty question.")
        my_session = _self_session(ctx)
        entry, resumed = await _ensure_host(
            target, session_dir=session_dir, my_session=my_session
        )
        try:
            reply = await remote.request_host(
                entry,
                "ask_aside",
                cleaned,
                model.strip(),
                timeout=ASIDE_TIMEOUT_SECONDS + 15.0,
            )
        except RemoteError as error:
            if error.type_name == "PermissionError":
                raise ModelRetry(_older_host_message(entry, "ask_aside")) from None
            raise ModelRetry(f"Host {entry.id} failed to answer: {error}") from None
        except (HostError, OSError) as error:
            raise ModelRetry(f"Could not reach host {entry.id}: {error}") from None
        reply["resumed"] = resumed
        return reply

    async def send_to_host(
        ctx: RunContext,
        target: str,
        prompt: str,
        wait: bool = False,
        timeout: float = 120.0,
    ) -> dict:
        """Send a prompt as a real turn on another session host.

        Accepts a running host ID or a saved session ID (auto-resuming in its workspace).
        Use `wait=False` (default) to kick off work in parallel, then `wait_for_hosts`.

        Args:
            target: Running host ID or saved session ID (or unique prefix).
            prompt: The prompt to queue on the target session.
            wait: Wait for the turn to finish and return its reply (default False).
            timeout: Seconds to wait when `wait=True`, 1-1800.
        """
        cleaned = prompt.strip()
        if not cleaned:
            raise ModelRetry("Provide a non-empty prompt.")
        if cleaned.startswith("!"):
            raise ModelRetry("Shell mode (`!command`) is not allowed remotely.")
        if not 1 <= timeout <= 1800:
            raise ModelRetry("Provide timeout between 1 and 1800 seconds.")
        my_session = _self_session(ctx)
        entry, resumed = await _ensure_host(
            target, session_dir=session_dir, my_session=my_session
        )

        if not wait:
            try:
                await remote.request_host(entry, "send", cleaned, timeout=15.0)
            except RemoteError as error:
                if error.type_name == "PermissionError":
                    raise ModelRetry(_older_host_message(entry, "send")) from None
                raise ModelRetry(f"Host {entry.id} rejected prompt: {error}") from None
            except (HostError, OSError) as error:
                raise ModelRetry(f"Could not send to host {entry.id}: {error}") from None
            return {
                "status": "queued",
                "host_id": entry.id,
                "session_id": entry.session_id,
                "workspace": entry.workspace,
                "resumed": resumed,
                "turns_before": entry.turns,
            }

        from pcode.gateway import send as gateway_send

        try:
            result = await asyncio.wait_for(gateway_send(entry, cleaned), timeout=timeout)
        except TimeoutError:
            return {
                "status": "still_running",
                "host_id": entry.id,
                "session_id": entry.session_id,
                "workspace": entry.workspace,
                "resumed": resumed,
                "note": (
                    f"Turn is still running after {timeout:g}s; "
                    "use wait_for_hosts or inspect_host to check on it."
                ),
            }
        except (HostError, RemoteError, OSError) as error:
            raise ModelRetry(f"Failed while running turn on host {entry.id}: {error}") from None
        return {
            "status": result.outcome,
            "host_id": entry.id,
            "session_id": entry.session_id,
            "workspace": entry.workspace,
            "resumed": resumed,
            "reply": result.reply,
            "notes": result.notes,
        }

    async def wait_for_hosts(
        ctx: RunContext,
        host_ids: list[str],
        mode: WaitMode = "all",
        timeout: float = 120.0,
    ) -> dict:
        """Wait for one or more running hosts to finish their active and queued turns.

        Args:
            host_ids: Running host IDs or session ID prefixes to wait on.
            mode: `all` waits for every host; `any` returns as soon as one finishes.
            timeout: Maximum seconds to wait, 1-1800.
        """
        if not host_ids:
            raise ModelRetry("Provide at least one host_id.")
        if not 1 <= timeout <= 1800:
            raise ModelRetry("Provide timeout between 1 and 1800 seconds.")
        my_session = _self_session(ctx)
        directory = host_dir()

        targets: list[HostEntry] = []
        completed: list[dict] = []
        seen_ids: set[str] = set()
        for raw in host_ids:
            selector = raw.strip()
            if not selector:
                continue
            entry = _find_running(selector, directory)
            if entry is None:
                completed.append({"host_id": selector, "state": "stopped"})
                continue
            if _is_self(entry, my_session):
                raise ModelRetry("Cannot wait on the current session.")
            if entry.id not in seen_ids:
                seen_ids.add(entry.id)
                targets.append(entry)

        pending = {entry.id: entry for entry in targets}
        deadline = time.monotonic() + timeout

        while pending:
            for identity, entry in list(pending.items()):
                current = _find_running(identity, directory)
                if current is None:
                    completed.append(
                        {
                            "host_id": identity,
                            "session_id": entry.session_id,
                            "state": "stopped",
                        }
                    )
                    pending.pop(identity, None)
                    continue
                try:
                    snap = await remote.request_host(current, "peek", timeout=5.0)
                except (HostError, RemoteError, OSError):
                    # Fall back to the on-disk HostEntry if peek fails or host is older.
                    if current.state == "idle":
                        completed.append(
                            {
                                "host_id": current.id,
                                "session_id": current.session_id,
                                "state": current.state,
                                "outcome": current.outcome,
                                "turns": current.turns,
                                "last_prompt": current.last_prompt,
                            }
                        )
                        pending.pop(identity, None)
                    continue
                if not _host_is_busy(snap):
                    completed.append(
                        {
                            "host_id": snap["id"],
                            "session_id": snap["session_id"],
                            "state": snap["state"],
                            "outcome": snap["outcome"],
                            "turns": snap["turns"],
                            "last_prompt": snap["last_prompt"],
                            "busy_with": snap["busy_with"],
                        }
                    )
                    pending.pop(identity, None)

            if not pending or (mode == "any" and completed):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.5, remaining))

        still_running = [
            {
                "host_id": entry.id,
                "session_id": entry.session_id,
                "state": entry.state,
                "last_prompt": entry.last_prompt,
            }
            for entry in pending.values()
        ]
        return {
            "completed": completed,
            "still_running": still_running,
            "timed_out": bool(pending) and not (mode == "any" and completed),
        }

    async def stop_host(
        ctx: RunContext,
        host_id: str,
        force: bool = False,
    ) -> dict:
        """Stop another running session host.

        When `force=False` (default), refuses if the host is busy or has an attached
        human terminal. Never deletes the session's worktree.

        Args:
            host_id: Running host ID or session ID prefix.
            force: Stop even if busy or a terminal is attached (default False).
        """
        selector = host_id.strip()
        if not selector:
            raise ModelRetry("Provide a host_id or session_id prefix.")
        my_session = _self_session(ctx)
        directory = host_dir()
        entry = _find_running(selector, directory)
        if entry is None:
            return {"status": "already_stopped", "host_id": selector}
        if _is_self(entry, my_session):
            raise ModelRetry("Cannot stop the current session.")

        if not force:
            attached = entry.attached
            try:
                snap = await remote.request_host(entry, "peek", timeout=5.0)
                attached = int(snap.get("attached", attached))
            except (HostError, RemoteError, OSError):
                pass
            if attached > 0:
                return {
                    "status": "refused",
                    "host_id": entry.id,
                    "session_id": entry.session_id,
                    "reason": (
                        f"{attached} terminal(s) attached; ask the user or pass force=True "
                        "to stop anyway."
                    ),
                }
            try:
                reason = await remote.stop_if_idle(entry)
            except (HostError, RemoteError, OSError) as error:
                raise ModelRetry(f"Could not stop host {entry.id}: {error}") from None
            if reason:
                return {
                    "status": "busy",
                    "host_id": entry.id,
                    "session_id": entry.session_id,
                    "reason": reason,
                }
            return {"status": "stopped", "host_id": entry.id, "session_id": entry.session_id}

        try:
            stopped = await remote.stop_entry(entry, keep_worktree=True)
        except (HostError, RemoteError, OSError) as error:
            raise ModelRetry(f"Could not stop host {entry.id}: {error}") from None
        return {
            "status": "stopped" if stopped else "stopping",
            "host_id": entry.id,
            "session_id": entry.session_id,
        }

    if pcode.is_worker:
        tools = [Tool(list_hosts, takes_ctx=True), Tool(inspect_host, takes_ctx=True)]
        instructions = WORKER_INSTRUCTIONS
    else:
        tools = [
            Tool(list_hosts, takes_ctx=True),
            Tool(inspect_host, takes_ctx=True),
            Tool(ask_host, takes_ctx=True),
            Tool(send_to_host, takes_ctx=True),
            Tool(wait_for_hosts, takes_ctx=True),
            Tool(stop_host, takes_ctx=True),
        ]
        instructions = PARENT_INSTRUCTIONS

    pcode.add_capability(Capability(id=pcode.id, tools=tools, instructions=instructions))
