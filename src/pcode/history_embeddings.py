"""Opt-in semantic ranking; cache vectors locally, never transcript text."""

import asyncio
import hashlib
import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path

from pcode.diagnostics import redact
from pcode.history import Chunk
from pcode.sessions import private_file

MAX_NEW_CHUNKS = 128
BATCH_SIZE = 32
CACHE_NAME = ".history-embeddings.sqlite3"


def _cache(path: Path, keys: list[str], updates: dict[str, list[float]] | None = None):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_file(path)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, vector TEXT NOT NULL)"
        )
        if updates:
            db.executemany(
                "INSERT OR REPLACE INTO vectors VALUES (?, ?)",
                [(key, json.dumps(vector)) for key, vector in updates.items()],
            )
        result = {}
        for key in keys:
            row = db.execute("SELECT vector FROM vectors WHERE key = ?", (key,)).fetchone()
            if row:
                result[key] = json.loads(row[0])
        return result


def _unit(vector) -> list[float]:
    values = [float(value) for value in vector]
    if not values or not all(math.isfinite(value) for value in values):
        raise ValueError("Invalid embedding vector")
    length = math.hypot(*values)
    if not length:
        raise ValueError("Empty embedding vector")
    return [value / length for value in values]


async def semantic_ranking(
    chunks: list[Chunk], query: str, model: str, root: Path, *, embedder=None
) -> tuple[list[int], list[str]]:
    """Incrementally index only this search's scope, bounded to 128 new chunks per call."""
    if not chunks:
        return [], []
    if embedder is None:
        from pydantic_ai import Embedder

        embedder = Embedder(model)
    # Include format/model in the key: changing either cannot reuse incompatible vectors.
    keys = [hashlib.sha256(f"v1\0{model}\0{c.text}".encode()).hexdigest() for c in chunks]
    path = root / CACHE_NAME
    cached = await asyncio.to_thread(_cache, path, keys)
    missing = list(dict.fromkeys(key for key in keys if key not in cached))
    texts = {key: chunk.text for key, chunk in zip(keys, chunks)}
    for start in range(0, min(len(missing), MAX_NEW_CHUNKS), BATCH_SIZE):
        batch = missing[start : min(start + BATCH_SIZE, MAX_NEW_CHUNKS)]
        result = await embedder.embed_documents([texts[key] for key in batch])
        if len(result.embeddings) != len(batch):
            raise ValueError("Embedding count mismatch")
        updates = {key: _unit(vector) for key, vector in zip(batch, result.embeddings)}
        await asyncio.to_thread(_cache, path, [], updates)
        cached.update(updates)
    result = await embedder.embed_query(redact(query))
    query_vector = _unit(result.embeddings[0])
    scored = []
    for index, key in enumerate(keys):
        if key not in cached:
            continue
        vector = _unit(cached[key])
        if len(vector) != len(query_vector):
            raise ValueError("Embedding dimensions changed; clear the history embedding cache")
        similarity = sum(a * b for a, b in zip(vector, query_vector))
        if similarity > 0:
            scored.append((similarity, index))
    warnings = []
    if len(missing) > MAX_NEW_CHUNKS:
        warnings.append(
            f"Semantic index is partial: {len(missing) - MAX_NEW_CHUNKS} chunks remain. "
            "Later semantic searches index more; keyword search covers the scanned corpus."
        )
    ranking, seen = [], set()
    for _, index in sorted(scored, key=lambda item: (-item[0], item[1])):
        chunk = chunks[index]
        identity = (chunk.session.id, chunk.turn.id)
        if identity in seen:
            continue
        seen.add(identity)
        ranking.append(index)
        if len(ranking) == 50:
            break
    return ranking, warnings
