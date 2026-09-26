"""Wire format and rendezvous for session hosts.

A session host is a headless pcode process that owns one conversation: the
agent, its tools, and the journal. Terminals attach to it over a Unix socket,
send prompts, and render the same `pcode.runtime` events a local turn yields.
Closing a terminal only detaches it; the host keeps working.

Messages are one JSON object per line. Every running host leaves
`<id>.sock` and `<id>.json` in `host_dir()`; the JSON is what `/switch` lists
without connecting to anything.
"""

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from pcode.runtime import (
    CacheBust,
    ChildPlan,
    ChildText,
    CommandOutput,
    EditCompleted,
    EditPreview,
    JobFinished,
    Message,
    PlanPreview,
    PlanUpdated,
    RunStatus,
    TextDelta,
    Thinking,
    ThinkingDelta,
    ToolStarted,
    ToolSummary,
)

# Bumped on any incompatible message change. An editable install lets a host
# outlive the code that started it, so a mismatch is refused, not guessed at.
PROTOCOL = 1

# asyncio's default line limit is 64 KiB, and one tool result or an attach
# snapshot of a long conversation is far larger than that.
LINE_LIMIT = 256 * 1024 * 1024

# macOS caps a Unix socket path (sun_path) at 104 bytes including the NUL.
SOCKET_PATH_MAX = 103

EVENT_TYPES = {
    cls.__name__: cls
    for cls in (
        Message,
        ToolStarted,
        ToolSummary,
        JobFinished,
        TextDelta,
        ThinkingDelta,
        Thinking,
        RunStatus,
        CacheBust,
        PlanUpdated,
        PlanPreview,
        ChildPlan,
        ChildText,
        CommandOutput,
        EditCompleted,
        EditPreview,
    )
}


def encode_event(event) -> dict:
    name = type(event).__name__
    if name not in EVENT_TYPES:
        raise TypeError(f"{name} is not a pcode runtime event")
    # Nested, not spread beside "kind" the way the journal writes events:
    # EditPreview has a `kind` field of its own that would overwrite it.
    return {"kind": name, "fields": asdict(event)}


def decode_event(data: dict):
    cls = EVENT_TYPES[data["kind"]]
    values = data["fields"]
    # Fields a newer host added are dropped rather than rejected.
    return cls(**{f.name: values[f.name] for f in fields(cls) if f.name in values})


def dumps(message: dict) -> bytes:
    return (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")


async def read_message(reader: asyncio.StreamReader) -> dict | None:
    """The next message, or None once the peer has gone."""
    try:
        line = await reader.readline()
    except (ConnectionError, asyncio.IncompleteReadError):
        return None
    if not line:
        return None
    message = json.loads(line)
    if not isinstance(message, dict):
        raise ValueError("Session host message is not an object.")
    return message


def host_dir() -> Path:
    if value := os.environ.get("PCODE_HOST_DIR"):
        return Path(value).expanduser()
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return state / "pcode" / "hosts"


def socket_path(identity: str, directory: Path | None = None) -> Path:
    path = (directory or host_dir()) / f"{identity}.sock"
    if len(os.fsencode(path)) > SOCKET_PATH_MAX:
        raise OSError(
            f"Socket path {path} is longer than a Unix socket allows; "
            "set PCODE_HOST_DIR to a shorter directory."
        )
    return path


@dataclass
class HostEntry:
    """What a running host says about itself, read without connecting to it."""

    id: str
    pid: int
    model: str
    workspace: str
    protocol: int = PROTOCOL
    session_id: str = ""
    # "starting", "idle", or "working".
    state: str = "starting"
    # The first prompt names the conversation; the last one says what it is doing.
    title: str = ""
    last_prompt: str = ""
    started: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    log: str = ""

    @property
    def socket(self) -> Path:
        return socket_path(self.id)

    def label(self) -> str:
        return self.title or "(no prompt yet)"


def write_entry(entry: HostEntry, directory: Path | None = None) -> None:
    directory = directory or host_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    entry.updated = time.time()
    target = directory / f"{entry.id}.json"
    temporary = target.with_suffix(".json.tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(asdict(entry), file)
    os.replace(temporary, target)


def remove_entry(identity: str, directory: Path | None = None) -> None:
    directory = directory or host_dir()
    for suffix in (".json", ".sock", ".json.tmp"):
        try:
            (directory / f"{identity}{suffix}").unlink()
        except FileNotFoundError:
            pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def list_hosts(directory: Path | None = None) -> list[HostEntry]:
    """Running hosts, most recently active first; a dead host's files are removed."""
    directory = directory or host_dir()
    entries = []
    if not directory.is_dir():
        return entries
    for path in directory.glob("*.json"):
        try:
            data = json.loads(path.read_text())
            entry = HostEntry(**{f.name: data[f.name] for f in fields(HostEntry) if f.name in data})
        except (OSError, ValueError, TypeError):
            continue
        if not _alive(entry.pid):
            remove_entry(entry.id, directory)
            continue
        entries.append(entry)
    return sorted(entries, key=lambda entry: entry.updated, reverse=True)


def find_host(selector: str, directory: Path | None = None) -> HostEntry:
    """A running host by ID or session ID prefix."""
    matches = [
        entry
        for entry in list_hosts(directory)
        if entry.id.startswith(selector)
        or (entry.session_id and entry.session_id.startswith(selector))
    ]
    if len(matches) != 1:
        raise LookupError(
            f"No running session host matches {selector!r}."
            if not matches
            else f"{selector!r} matches {len(matches)} session hosts; use more characters."
        )
    return matches[0]
