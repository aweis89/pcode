"""What one listener has handled, routed and sent: one JSON file, rewritten atomically.

The file belongs to one invocation. A new `--email-listen` starts it afresh:
the old alias is dead, so nothing addressed to it could be accepted, and an
old queue is never drained or resumed. It holds the token's hash, never the
token, and lives in a `0700` directory the remote hosts' sandbox cannot read.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Route targets that are not a session: a message whose replies start one.
LAUNCHER, CONTROL = "launcher", "control"


def state_dir() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return state / "pcode" / "email-remote"


@dataclass
class OutboxEntry:
    """A reply as composed, so a retry resends it, same Message-ID, without a new turn.

    Its parts rather than the message: the message carries the alias, which is
    never written to disk, so a retry composes it again around the same text.
    """

    planned_rfc_id: str
    session: str
    in_reply_to: str
    subject: str
    body: str
    references: list[str] = field(default_factory=list)
    auto: str = "auto-replied"
    attempts: int = 0
    # "pending", "sending", "sent", or "failed".
    state: str = "pending"
    # When a failed send may be tried again (the listener's clock).
    retry_at: float = 0.0


@dataclass
class State:
    path: Path
    listener: dict = field(default_factory=dict)
    # Gmail message id -> disposition ("accepted" or a rejection code).
    handled: dict[str, str] = field(default_factory=dict)
    # RFC Message-ID -> session key, LAUNCHER, or CONTROL.
    routes: dict[str, str] = field(default_factory=dict)
    # Session key -> what reaches it again (host id, session id, worktree, base).
    sessions: dict[str, dict] = field(default_factory=dict)
    outbox: list[OutboxEntry] = field(default_factory=list)

    def save(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        data = {key: value for key, value in asdict(self).items() if key != "path"}
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".state-")
        try:
            with os.fdopen(fd, "w") as file:
                json.dump(data, file, indent=1, sort_keys=True)
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    @classmethod
    def load(cls, path: Path) -> State:
        data = json.loads(path.read_text())
        outbox = [OutboxEntry(**entry) for entry in data.pop("outbox", [])]
        return cls(path=path, outbox=outbox, **data)
