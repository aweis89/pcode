"""Private session metadata/journal around Harness's native step persistence."""

import json
import os
import re
import tempfile
from collections import deque
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from filelock import FileLock, Timeout
from pydantic import BaseModel
from pydantic_ai_harness.step_persistence import SqliteStepStore, StepEvent, ToolEffectRecord

from pcode.conversation_tree import ConversationTree
from pcode.diagnostics import redact, versions


class SessionError(ValueError):
    pass


class SessionInfo(BaseModel):
    version: Literal[1] = 1
    id: str
    model: str
    workspace: str
    created: str
    updated: str
    status: str = "new"
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # Defaulted, so a session written before cache accounting still loads.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    packages: dict[str, str]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def session_root() -> Path:
    if value := os.environ.get("PCODE_SESSION_DIR"):
        return Path(value).expanduser()
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state / "pcode" / "sessions"


def private_file(path: Path) -> None:
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def read_info(path: Path) -> SessionInfo:
    try:
        info = SessionInfo.model_validate_json((path / "session.json").read_text())
    except (OSError, ValueError):
        raise SessionError(
            "Session metadata is unreadable or uses an unsupported format."
        ) from None
    if info.id != path.name:
        raise SessionError("Session ID does not match its directory.")
    return info


def list_sessions(root: Path | None = None) -> list[SessionInfo]:
    root = root or session_root()
    result = []
    if root.is_dir():
        for path in root.iterdir():
            if path.is_dir() and not path.is_symlink():
                try:
                    result.append(read_info(path))
                except SessionError:
                    continue
    return sorted(result, key=lambda info: info.updated, reverse=True)


def first_prompt(info: SessionInfo, root: Path | None = None) -> str:
    """Read the first submitted prompt without opening or locking the session."""
    directory = (root or session_root()) / info.id
    path = directory / "transcript.jsonl"
    if directory.is_symlink() or path.is_symlink():
        return "(Prompt unavailable)"
    try:
        with path.open() as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and record.get("kind") == "turn_started":
                    prompt = record.get("prompt")
                    if isinstance(prompt, str):
                        return prompt
    except OSError:
        return "(Prompt unavailable)"
    return "(No prompt yet)"


def resolve_session(selector: str, root: Path | None = None) -> Path:
    root = root or session_root()
    if selector != "latest" and not re.fullmatch(r"[a-f0-9-]{8,36}", selector):
        raise SessionError("Use a session ID from --sessions, or 'latest'.")
    sessions = list_sessions(root)
    matches = (
        sessions[:1] if selector == "latest" else [s for s in sessions if s.id.startswith(selector)]
    )
    if len(matches) != 1:
        raise SessionError("Session not found or prefix is ambiguous; use --sessions.")
    return root / matches[0].id


class PrivateStepStore(SqliteStepStore):
    """Use the public store hooks to redact error strings, not replay-critical history."""

    async def append_event(self, event: StepEvent) -> None:
        if event.error:
            event = replace(event, error=redact(event.error))
        await super().append_event(event)

    async def record_tool_effect(self, record: ToolEffectRecord) -> None:
        if record.effect_summary:
            record = replace(record, effect_summary=redact(record.effect_summary))
        await super().record_tool_effect(record)


class SavedSession:
    def __init__(self, directory: Path, info: SessionInfo) -> None:
        if directory.is_symlink():
            raise SessionError("Refusing a symlinked session directory.")
        self.directory = directory
        directory.chmod(0o700)
        self.info = info
        self.lock = FileLock(directory / ".lock", mode=0o600)
        try:
            self.lock.acquire(timeout=0)
        except Timeout:
            raise SessionError("This session is already open in another process.") from None
        try:
            for name in ("steps.sqlite3", "transcript.jsonl", "session.json"):
                private_file(directory / name)
            self.store = PrivateStepStore(database=directory / "steps.sqlite3")
            self.tree = ConversationTree()
            for record in self.records():
                self.tree.consume(record)
        except BaseException:
            self.lock.release()
            raise

    @classmethod
    def create(cls, model: str, workspace: Path, root: Path | None = None):
        root = root or session_root()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        identity = str(uuid4())
        directory = root / identity
        directory.mkdir(mode=0o700)
        info = SessionInfo(
            id=identity,
            model=model,
            workspace=str(workspace.resolve()),
            created=now(),
            updated=now(),
            packages=versions(),
        )
        session = cls(directory, info)
        try:
            session.save_info()
        except BaseException:
            session.close()
            raise
        return session

    @classmethod
    def open(cls, selector: str, root: Path | None = None):
        path = resolve_session(selector, root)
        return cls(path, read_info(path))

    def save_info(self) -> None:
        self.info.updated = now()
        # Atomic replacement: interruption cannot leave half a manifest.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.directory, delete=False) as file:
                temporary = Path(file.name)
                file.write(self.info.model_dump_json(indent=2))
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(self.directory / "session.json")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def append(self, kind: str, *, sync: bool = False, **data) -> None:
        record = {"version": 1, "time": now(), "kind": kind, **data}
        # Conversation content is intentionally retained losslessly. The directory
        # is private; credentials/transport headers are never passed to this API.
        with (self.directory / "transcript.jsonl").open("ab+") as file:
            if file.tell():
                file.seek(-1, os.SEEK_END)
                if file.read(1) != b"\n":
                    # Preserve a torn final record for debugging, but don't glue
                    # the next valid event onto it after a restart.
                    file.write(b"\n")
            file.write((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
            file.flush()
            if sync:
                os.fsync(file.fileno())

        self.tree.consume(record)

    def event(self, event) -> None:
        self.append(type(event).__name__, **asdict(event))

    def records(self):
        """Read intact records; a hard kill may leave a torn final append."""
        with (self.directory / "transcript.jsonl").open(encoding="utf-8", errors="replace") as file:
            for line in file:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    yield record

    def active_records(self):
        selected = set(self.tree.path(self.tree.active))
        run_id = None
        for record in self.records():
            if record.get("kind") == "turn_started":
                run_id = record.get("run_id")
            if not self.tree.nodes or run_id in selected:
                yield record

    async def history_at(self, identity: str | None):
        path = self.tree.path(identity)
        for run_id in reversed(path):
            node = self.tree.nodes[run_id]
            if node.kind == "compaction":
                return deepcopy(node.history)
            # Never opt into interrupted snapshots with pending tool calls.
            snapshot = await self.store.latest_snapshot(run_id=run_id)
            if snapshot is not None:
                return snapshot.messages
            if self.tree.nodes[run_id].status == "completed":
                raise SessionError("The selected turn's checkpoint is missing; branch unchanged.")
        return []

    async def recover(self):
        """Restore settled history without replaying tools or resolving their effects.

        Interrupted tools may have changed the workspace. Keep their ledger entries
        intact for diagnostics, but do not require review to continue the session.
        """
        if self.tree.nodes:
            return await self.history_at(self.tree.active)
        # Compatibility with sessions whose journal predates run IDs. Delegated
        # runs are stored too, and a sub-agent's history is not this conversation.
        runs = [
            run
            for run in await self.store.list_runs(conversation_id=self.info.id)
            if run.parent_run_id is None
        ]
        for run in reversed(runs):
            snapshot = await self.store.latest_snapshot(run_id=run.run_id)
            if snapshot is not None:
                return snapshot.messages
        return []

    def latest_plan(self) -> list[dict]:
        """Recover UI/tool state even when the last update predates replay's limit."""
        if self.tree.nodes:
            return deepcopy(self.tree.nodes[self.tree.active].plan) if self.tree.active else []
        items = []
        for record in self.active_records():
            if record.get("kind") == "PlanUpdated":
                items = record["items"]
        return items

    def tool_events(self):
        """Stream tool lifecycle records independently of the transcript replay limit."""
        for record in self.active_records():
            if record.get("kind") in {
                "ToolStarted",
                "ToolSummary",
                "turn_started",
                "turn_completed",
                "turn_cancelled",
                "turn_failed",
            }:
                yield record

    def recent_transcript(self, limit: int = 40) -> list[dict]:
        """UI replay, distinct from the complete model history stored by Harness."""
        records = deque(maxlen=limit)
        partial = ""
        thinking = None
        for record in self.active_records():
            kind = record.get("kind")
            if kind == "ThinkingDelta":
                if thinking is None:
                    thinking = {"kind": "thinking_partial", "text": ""}
                    records.append(thinking)
                thinking["text"] += record["text"]
            elif kind == "Thinking":
                if thinking is None:
                    records.append(record)
                else:
                    # Update in place: unrelated tool/status records may have
                    # arrived while the block was streaming. Never duplicate it.
                    thinking.update(record)
                thinking = None
            elif kind == "TextDelta":
                thinking = None
                partial += record["text"]
            elif kind == "Message":
                partial = ""
                records.append(record)
            elif kind in ("turn_failed", "turn_cancelled", "turn_started"):
                thinking = None
                if partial:
                    records.append({"kind": "partial", "markdown": partial})
                    partial = ""
                records.append(record)
            elif kind in ("ToolSummary", "EditCompleted", "CacheBust"):
                records.append(record)
        if partial:
            records.append({"kind": "partial", "markdown": partial})
        return list(records)

    def close(self) -> None:
        self.lock.release()
