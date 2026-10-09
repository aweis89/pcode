"""The persisted index of fork points that lets a restarted pcode resume warm."""

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

logger = logging.getLogger(__name__)

INDEX_LIMIT = 4000


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
            except ValueError, KeyError, TypeError, AttributeError:
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
