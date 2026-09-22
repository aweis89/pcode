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
    "auto_compacted",
    "steering",
)
CHUNK_CHARS = 4000
MAX_CHUNKS = 10_000
# Journal bytes read, not searchable text: most of a transcript is streaming
# deltas that the record filter discards. Scanning is ~75 MB/s on a warm cache,
# so this bounds a search at a few seconds; `after` continues past it.
MAX_SCAN_BYTES = 256 * 1024 * 1024
# Ranking is chunk-level, so one long session can otherwise fill the whole limit.
PER_SESSION_LIMIT = 3
# Enough occurrences to find prose without scanning a pathological chunk repeatedly.
MAX_MATCH_POSITIONS = 200
EXCERPT_CHARS = 800
CONCLUSION_CHARS = 240


@dataclass
class HistoryTurn:
    id: str
    parent: str | None
    time: str
    status: str = "incomplete"
    active: bool | None = False
    # Auto-compaction dropped part of this turn from the model's own context,
    # which makes the journal the only copy even while the turn is still running.
    compacted: bool = False
    blocks: list[str] = field(default_factory=list)

    @property
    def branch(self) -> str:
        return "unknown" if self.active is None else "active" if self.active else "inactive"

    @property
    def text(self) -> str:
        return redact("\n\n".join(self.blocks))

    @property
    def conclusion(self) -> str:
        """The last assistant text: what the turn decided, not how it got there."""
        for block in reversed(self.blocks):
            if block.startswith("Assistant: "):
                return redact(block.removeprefix("Assistant: ")).strip()
        return ""


@dataclass
class Chunk:
    session: SessionInfo
    turn: HistoryTurn
    offset: int
    text: str

    def excerpt_start(self, query: str) -> int:
        """Anchor on the densest match in prose: tool lines rarely hold the answer."""
        words = list(dict.fromkeys(query.casefold().split()))
        folded = self.text.casefold()
        positions = sorted(
            {
                match.start()
                for word in words
                for match in list(re.finditer(re.escape(word), folded))[:MAX_MATCH_POSITIONS]
            }
        )
        if not positions:
            return 0
        candidates = [
            position
            for position in positions
            if not self.text.startswith("Tool: ", self.text.rfind("\n", 0, position) + 1)
        ]

        def density(position: int) -> int:
            window = folded[position : position + EXCERPT_CHARS]
            return sum(word in window for word in words)

        # Most distinct terms in the window the excerpt would show; ties go earlier.
        best = max(candidates or positions, key=lambda position: (density(position), -position))
        return max(0, best - 150)

    def result(self, query: str) -> dict:
        start = self.excerpt_start(query)
        excerpt = self.text[start : start + EXCERPT_CHARS]
        result = {
            "turn_id": self.turn.id,
            "time": self.turn.time,
            "status": self.turn.status,
            "branch": self.turn.branch,
            "offset": self.offset + start,
            "excerpt": excerpt,
        }
        # "Did we already do X" is answered by the turn's last word on it, which
        # a match in the middle of a long turn would otherwise bury.
        conclusion = self.turn.conclusion[:CONCLUSION_CHARS]
        if conclusion and conclusion not in excerpt:
            result["conclusion"] = conclusion
        return result


@dataclass
class Scan:
    """Searchable chunks plus the coverage of the scan that produced them."""

    chunks: list[Chunk]
    warnings: list[str]
    sessions_in_scope: int = 0
    sessions_searched: int = 0
    sessions_unreadable: int = 0
    sessions_partial: int = 0
    sessions_before: int = 0
    next_cursor: str | None = None


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
            elif kind == "auto_compacted":
                turn.compacted = True
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

    def chunks(
        self, scope: Scope, *, exclude_turn: str | None = None, after: str | None = None
    ) -> "Scan":
        """Searchable text in scope, with how much of the scope it actually covers.

        Sessions are newest first, so a budget that runs out always cuts off the
        older end. `after` resumes from the session that cut off, which is how a
        question about older work reaches history the first scan never read.
        """
        sessions = self.sessions(scope)
        scan = Scan([], [], sessions_in_scope=len(sessions))
        if after:
            identities = [info.id for info in sessions]
            if after not in identities:
                raise ValueError("Unknown cursor; use next_cursor from an earlier search.")
            scan.sessions_before = identities.index(after) + 1
            sessions = sessions[scan.sessions_before :]
        budget = SessionReadBudget(MAX_SCAN_BYTES)
        last = after
        full = False
        for info in sessions:
            if full or budget.exhausted:
                break
            try:
                turns = read_turns(info, self.root, budget=budget)
            except OSError:
                scan.warnings.append(f"Skipped unreadable session {info.id}.")
                scan.sessions_unreadable += 1
                last = info.id
                continue
            complete = not budget.exhausted
            if complete:
                scan.sessions_searched += 1
            # A session cut off mid-way stays behind the cursor, so the next page
            # re-reads it whole. Unless it used up the page by itself: then the
            # cursor has to pass it or continuing would never move.
            if complete or not (scan.sessions_searched or scan.sessions_unreadable):
                last = info.id
            if not complete:
                scan.sessions_partial += 1
                scan.warnings.append(
                    f"Journal scan byte budget reached inside session {info.id}; its later "
                    "records were not searched and its branch labels are unknown."
                )
            for turn in reversed(list(turns.values())):
                if turn.id == exclude_turn and info.id == self.session_id and not turn.compacted:
                    continue
                text = turn.text
                for offset in range(0, len(text), CHUNK_CHARS - 200):
                    if len(scan.chunks) == MAX_CHUNKS:
                        scan.warnings.append(f"Search limited to {MAX_CHUNKS} chunks.")
                        full = True
                        break
                    scan.chunks.append(
                        Chunk(info, turn, offset, text[offset : offset + CHUNK_CHARS])
                    )
                if full:
                    break
        read = scan.sessions_before + scan.sessions_searched + scan.sessions_unreadable
        if read < scan.sessions_in_scope:
            # Sessions are ordered newest first, so the gap is always the older ones.
            scan.next_cursor = last
            scan.warnings.append(
                f"Searched {scan.sessions_searched} of {scan.sessions_in_scope} sessions in "
                f"scope in full; {scan.sessions_in_scope - read} older ones were not "
                f'searched. Pass after="{last}" to continue there if the answer may be older.'
            )
        scan.warnings = scan.warnings[-10:]
        return scan

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


def group_results(
    chunks: list[Chunk],
    ranking: list[int],
    query: str,
    limit: int,
    current_session: str | None = None,
    current_turn: str | None = None,
) -> list[dict]:
    """Group hits by session, spreading a small limit across sessions first.

    A long session produces many chunks and can otherwise take every slot, so
    the first pass caps each session and a second pass spends whatever is left.
    """
    ordered: list[Chunk] = []
    seen: set[tuple[str, str]] = set()
    for index in ranking:
        chunk = chunks[index]
        key = (chunk.session.id, chunk.turn.id)
        if key not in seen:
            seen.add(key)
            ordered.append(chunk)
    picked: set[int] = set()
    counts: Counter[str] = Counter()
    for cap in (PER_SESSION_LIMIT, len(ordered)):
        for position, chunk in enumerate(ordered):
            if len(picked) == limit:
                break
            if position in picked or counts[chunk.session.id] >= cap:
                continue
            picked.add(position)
            counts[chunk.session.id] += 1
    groups: dict[str, dict] = {}
    for position in sorted(picked):
        chunk = ordered[position]
        group = groups.setdefault(
            chunk.session.id,
            {
                "session_id": chunk.session.id,
                "workspace": redact(chunk.session.workspace),
                "current": chunk.session.id == current_session,
                "turns": [],
            },
        )
        result = chunk.result(query)
        if chunk.turn.id == current_turn and chunk.session.id == current_session:
            # Only survives the scan when auto-compaction took it out of context:
            # this is the turn asking, recovering its own dropped history.
            result["current_turn"] = True
        group["turns"].append(result)
    return list(groups.values())


def merge_rankings(keyword: list[int], semantic: list[int]) -> list[int]:
    """Reciprocal-rank fusion keeps exact matches useful alongside semantic recall."""
    scores: dict[int, float] = {}
    for ranking in (keyword, semantic):
        for rank, index in enumerate(ranking):
            scores[index] = scores.get(index, 0) + 1 / (60 + rank)
    return sorted(scores, key=lambda index: (-scores[index], index))
