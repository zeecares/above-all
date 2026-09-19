"""Small, offline hybrid retrieval without a model download.

This is deliberately an experiment rather than the default.  The embedding is
an in-process signed feature hash over word and character n-grams.  It has no
weights, tokenizer package, network path, or transitive runtime dependency.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from itertools import pairwise

_DIMENSIONS = 256
_TOKEN = re.compile(r"[\w]+", re.UNICODE)


def _features(text: str):
    words = [word.casefold() for word in _TOKEN.findall(text)]
    for word in words:
        yield "w:" + word
        padded = f"  {word}  "
        for size in (3, 4, 5):
            for offset in range(max(0, len(padded) - size + 1)):
                yield f"c{size}:" + padded[offset : offset + size]
    for left, right in pairwise(words):
        yield f"b:{left}:{right}"


def embed(text: str) -> list[float]:
    vector = [0.0] * _DIMENSIONS
    for feature in _features(text):
        digest = hashlib.blake2b(feature.encode(), digest_size=8, person=b"aboveall").digest()
        value = int.from_bytes(digest, "big")
        vector[value % _DIMENSIONS] += 1.0 if value & 1 else -1.0
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


def cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def sync_embedding(db, note_id: str, title: str, body: str, *, active: bool) -> None:
    db.execute("DELETE FROM note_embeddings WHERE note_id=?", (note_id,))
    if active:
        db.execute(
            "INSERT INTO note_embeddings(note_id,vector_json,embedded_at) VALUES(?,?,?)",
            (note_id, json.dumps(embed(f"{title}\n{body}"), separators=(",", ":")), datetime.now(timezone.utc).isoformat()),
        )


def semantic_rows(db, query: str, limit: int = 20) -> list[dict]:
    today = datetime.now(timezone.utc).date().isoformat()
    query_vector = embed(query)
    rows = db.execute(
        "SELECT n.id,n.title,n.body,n.path,e.vector_json FROM note_embeddings e "
        "JOIN notes n ON n.id=e.note_id WHERE n.status='active' "
        "AND (n.stale_after IS NULL OR n.stale_after >= ?)", (today,)
    )
    scored = [(cosine(query_vector, json.loads(row["vector_json"])), dict(row)) for row in rows]
    scored.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
    return [{k: v for k, v in row.items() if k != "vector_json"} for score, row in scored[:limit] if score > 0]


def reciprocal_rank_fusion(*rankings: list[dict], limit: int = 20, rank_constant: int = 60) -> list[dict]:
    scores: dict[str, float] = {}
    rows: dict[str, dict] = {}
    for ranking in rankings:
        for rank, row in enumerate(ranking, 1):
            note_id = row["id"]
            rows[note_id] = row
            scores[note_id] = scores.get(note_id, 0.0) + 1.0 / (rank_constant + rank)
    ordered = sorted(scores, key=lambda note_id: (-scores[note_id], note_id))[:limit]
    return [{**rows[note_id], "rrf_score": scores[note_id]} for note_id in ordered]
