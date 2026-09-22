"""Read-only, bounded retrieval over saved conversations, independent of the UI."""

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pcode.diagnostics import redact
from pcode.sessions import SessionInfo, SessionReadBudget, list_sessions, session_records
from pcode.worktree import WORKTREES_DIR, project_checkout

Scope = Literal["session", "project", "workspace", "all"]
KINDS = (
    "turn_started",
    "Message",
    "ToolSummary",
    "turn_completed",
    "turn_failed",
    "turn_cancelled",
    "tree_selected",
    "compaction_checkpoint",
    "steering",
)
CHUNK_CHARS = 4000
MAX_CHUNKS = 10_000
MAX_SCAN_BYTES = 64 * 1024 * 1024


@dataclass
class HistoryTurn:
    id: str
    parent: str | None
    time: str
    status: str = "incomplete"
    active: bool | None = False
    blocks: list[str] = field(default_factory=list)

    @property
    def branch(self) -> str:
        return "unknown" if self.active is None else "active" if self.active else "inactive"

    @property
    def text(self) -> str:
        return redact("\n\n".join(self.blocks))


@dataclass
class Chunk:
    session: SessionInfo
    turn: HistoryTurn
    offset: int
    text: str

    def result(self, query: str) -> dict:
        words = query.casefold().split()
        folded = self.text.casefold()
        position = min((folded.find(w) for w in words if w in folded), default=0)
        start = max(0, position - 150)
        return {
            "session_id": self.session.id,
            "turn_id": self.turn.id,
            "time": self.turn.time,
            "workspace": redact(self.session.workspace),
            "status": self.turn.status,
            "branch": self.turn.branch,
            "offset": self.offset + start,
            "excerpt": self.text[start : start + 800],
        }


def _turn_of(record: dict, turns: dict[str, "HistoryTurn"], recording: str | None):
    """The turn a record belongs to: the one it names, else the last one started.

    The fallback is for journals written before records carried a run ID, and
    for a scan window that began after the turn's own start record.
    """
    identity = record.get("run_id")
    if not isinstance(identity, str) or identity not in turns:
        identity = recording
    return turns.get(identity) if identity is not None else None


def read_turns(
    info: SessionInfo, root: Path, *, budget: SessionReadBudget | None = None
) -> dict[str, HistoryTurn]:
    """Keep run IDs and branch ancestry without locking or loading model checkpoints."""
    budget = budget or SessionReadBudget(MAX_SCAN_BYTES)
    turns: dict[str, HistoryTurn] = {}
    parents: dict[str, str | None] = {}
    active = recording = None
    for record in session_records(info, root, kinds=KINDS, budget=budget):
        kind = record["kind"]
        if kind == "turn_started" and isinstance(record.get("prompt"), str):
            identity = record.get("run_id") or f"turn-{len(turns) + 1}"
            parent = record.get("parent_id", active)
            if not isinstance(identity, str) or not (parent is None or isinstance(parent, str)):
                continue
            parents[identity] = parent
            turns[identity] = HistoryTurn(
                identity,
                parent,
                str(record.get("time", info.created)),
                blocks=["User: " + record["prompt"]],
            )
            active = recording = identity
        elif kind == "compaction_checkpoint":
            identity, parent = record.get("node_id"), record.get("parent_id")
            if isinstance(identity, str) and (parent is None or isinstance(parent, str)):
                parents[identity] = parent
                active, recording = identity, None
        elif kind == "tree_selected":
            identity = record.get("node_id")
            if identity is None or isinstance(identity, str):
                active = identity
        elif (turn := _turn_of(record, turns, recording)) is not None:
            if kind == "steering" and isinstance(record.get("prompt"), str):
                turn.blocks.append("User (steering): " + record["prompt"])
            elif kind == "Message" and isinstance(record.get("markdown"), str):
                turn.blocks.append("Assistant: " + record["markdown"])
            elif kind == "ToolSummary":
                # No full results: they are noisy, often huge, and can hold private data.
                turn.blocks.append(
                    "Tool: "
                    + " ".join(str(record.get(k) or "") for k in ("name", "detail", "command"))
                    + (" [failed]" if record.get("failed") else "")
                )
            elif kind in {"turn_completed", "turn_failed", "turn_cancelled"}:
                turn.status = kind.removeprefix("turn_")
    seen = set()
    while active in parents and active not in seen:
        seen.add(active)
        if active in turns:
            turns[active].active = True
        active = parents[active]
    # Resolve compaction nodes to the nearest actual turn for context retrieval.
    for turn in turns.values():
        if budget.exhausted:
            turn.active = None  # Later selections/forks may be outside the scanned prefix.
        seen = set()
        while turn.parent in parents and turn.parent not in turns and turn.parent not in seen:
            seen.add(turn.parent)
            turn.parent = parents[turn.parent]
    return turns


def project_path(workspace: Path) -> Path:
    workspace = workspace.resolve()
    if workspace.exists():
        project = project_checkout(workspace)
        if project:
            return project
    # Older metadata did not save project identity. Recover pcode's conventional
    # worktree layout even after deletion; arbitrary deleted worktrees stay separate.
    for parent in workspace.parents:
        if parent.name == WORKTREES_DIR:
            candidate = parent.parent
            if (candidate / ".git").exists():
                return candidate
    return workspace


class History:
    def __init__(self, workspace: Path, root: Path, session_id: str | None = None):
        self.workspace = workspace.resolve()
        self.root = root
        self.session_id = session_id

    def sessions(self, scope: Scope) -> list[SessionInfo]:
        if scope not in ("session", "project", "workspace", "all"):
            raise ValueError("scope must be session, project, workspace, or all")
        records = list_sessions(self.root)
        if scope == "session":
            return [s for s in records if s.id == self.session_id]
        if scope == "all":
            return records
        if scope == "workspace":
            return [s for s in records if Path(s.workspace).resolve() == self.workspace]
        project = project_path(self.workspace)
        projects: dict[tuple[str | None, str], Path] = {}
        result = []
        for info in records:
            key = (info.project, info.workspace)
            if key not in projects:
                projects[key] = (
                    Path(info.project).resolve()
                    if info.project
                    else project_path(Path(info.workspace))
                )
            if projects[key] == project:
                result.append(info)
        return result

    def chunks(self, scope: Scope) -> tuple[list[Chunk], list[str]]:
        chunks = []
        warnings = []
        budget = SessionReadBudget(MAX_SCAN_BYTES)
        for info in self.sessions(scope):
            try:
                turns = read_turns(info, self.root, budget=budget)
            except OSError:
                warnings.append(f"Skipped unreadable session {info.id}.")
                continue
            if budget.exhausted:
                warnings.append(
                    "Journal scan byte budget reached; later records/sessions were not searched. "
                    "Branch labels in the partial session are unknown."
                )
            for turn in reversed(list(turns.values())):
                text = turn.text
                for offset in range(0, len(text), CHUNK_CHARS - 200):
                    if len(chunks) == MAX_CHUNKS:
                        return chunks, [
                            *warnings[:10],
                            f"Search limited to {MAX_CHUNKS} chunks.",
                        ]
                    chunks.append(Chunk(info, turn, offset, text[offset : offset + CHUNK_CHARS]))
            if budget.exhausted:
                break
        return chunks, warnings[-10:]

    def read(
        self,
        session_id: str,
        turn_id: str,
        scope: Scope = "project",
        context_turns: int = 1,
        offset: int = 0,
        max_chars: int = 8000,
    ) -> dict:
        if not 0 <= context_turns <= 3 or offset < 0 or not 1 <= max_chars <= 16_000:
            raise ValueError("Use context_turns 0..3, offset >= 0, and max_chars 1..16000")
        info = next((s for s in self.sessions(scope) if s.id == session_id), None)
        if info is None:
            raise ValueError(
                "Session not found in scope. Use an exact session_id from search_sessions."
            )
        budget = SessionReadBudget(MAX_SCAN_BYTES)
        try:
            turns = read_turns(info, self.root, budget=budget)
        except OSError:
            raise ValueError("Session transcript is unreadable.") from None
        if turn_id not in turns:
            if budget.exhausted:
                raise ValueError("Turn not found within the journal scan byte budget.")
            raise ValueError("Turn not found. Use a turn_id from search_sessions.")
        turn = turns[turn_id]
        text = turn.text
        if offset > len(text):
            raise ValueError("offset exceeds the turn's text length")
        context = []
        parent, seen = turn.parent, {turn.id}
        while parent in turns and parent not in seen and len(context) < context_turns:
            previous = turns[parent]
            seen.add(parent)
            context.append(
                {"turn_id": parent, "status": previous.status, "text": previous.text[:1000]}
            )
            parent = previous.parent
        end = min(offset + max_chars, len(text))
        return {
            "session_id": info.id,
            "turn_id": turn.id,
            "time": turn.time,
            "workspace": redact(info.workspace),
            "status": turn.status,
            "branch": turn.branch,
            "warnings": ["Journal scan byte budget reached; later records were not read."]
            if budget.exhausted
            else [],
            "context": context[::-1],
            "text": text[offset:end],
            "offset": offset,
            "next_offset": end if end < len(text) else None,
        }


_TOKEN = re.compile(r"\w+")
# Harness ConversationSearch's defaults: k1 above Lucene's 1.2 favors repeated terms.
BM25_K1 = 1.5
BM25_B = 0.75
PHRASE_BONUS = 1.0


def _tokenize(text: str) -> list[str]:
    return [match.group().casefold() for match in _TOKEN.finditer(text)]


def keyword_ranking(chunks: list[Chunk], query: str) -> list[int]:
    """BM25 over chunks, ported from Harness ConversationSearch, plus an exact-phrase bonus.

    Rare terms outrank common ones and long chunks are length-normalized, so
    a query like "editor flicker" finds the turn about it rather than the
    turn that merely mentions "editor" the most times.
    """
    query_tokens = list(dict.fromkeys(_tokenize(query)))
    if not query_tokens or not chunks:
        return []
    documents = [_tokenize(chunk.text) for chunk in chunks]
    avgdl = sum(len(tokens) for tokens in documents) / len(documents)
    if not avgdl:
        return []
    frequencies = [Counter(tokens) for tokens in documents]
    total = len(documents)
    idf = {}
    for term in query_tokens:
        df = sum(1 for counts in frequencies if term in counts)
        idf[term] = math.log((total - df + 0.5) / (df + 0.5) + 1.0) if df else 0.0
    phrase = " ".join(query.casefold().split())
    scored = []
    for index, (counts, chunk) in enumerate(zip(frequencies, chunks)):
        dl = sum(counts.values())
        score = 0.0
        for term in query_tokens:
            tf = counts.get(term, 0)
            if tf:
                score += (
                    idf[term]
                    * tf
                    * (BM25_K1 + 1.0)
                    / (tf + BM25_K1 * (1.0 - BM25_B + BM25_B * dl / avgdl))
                )
        if score > 0 and phrase in " ".join(chunk.text.casefold().split()):
            score += PHRASE_BONUS
        if score > 0:
            scored.append((score, index))
    return [index for _, index in sorted(scored, key=lambda item: (-item[0], item[1]))]


def merge_rankings(keyword: list[int], semantic: list[int]) -> list[int]:
    """Reciprocal-rank fusion keeps exact matches useful alongside semantic recall."""
    scores: dict[int, float] = {}
    for ranking in (keyword, semantic):
        for rank, index in enumerate(ranking):
            scores[index] = scores.get(index, 0) + 1 / (60 + rank)
    return sorted(scores, key=lambda index: (-scores[index], index))
