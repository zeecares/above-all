"""Reviewed, provenance-backed knowledge map stored beside note search in SQLite."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .notes import Note


def _scope(path: Path) -> str:
    return "project" if path.parent.parent.name == ".above-all" else "global"


def _claim_text(note: Note) -> str:
    lines = note.body.strip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _id(value: str) -> str:
    value = re.sub(r"[^a-z0-9_-]+", "-", value.casefold()).strip("-")
    if not value:
        raise ValueError("knowledge-map identifiers must contain a letter or number")
    return value


def sync_note_map(db: sqlite3.Connection, path: Path, note: Note) -> None:
    """Mirror one reviewed note into the map; this never promotes a candidate."""
    note_id = path.stem
    scope = str(note.metadata.get("scope") or _scope(path))
    if scope not in {"global", "project"}:
        raise ValueError("knowledge-map scope must be global or project")
    kind = str(note.metadata.get("claim_kind", "premise"))
    if kind not in {"premise", "inference"}:
        raise ValueError("claim_kind must be premise or inference")
    confidence = float(note.metadata.get("confidence", 1.0 if kind == "premise" else 0.5))
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence must be between 0 and 1")
    status = str(note.metadata["status"])
    stale_after = str(note.metadata["stale_after"]) if note.metadata.get("stale_after") else None
    claim_id = f"note:{note_id}"
    now = datetime.now(timezone.utc).isoformat()

    db.execute("DELETE FROM knowledge_relations WHERE note_id=?", (note_id,))
    db.execute("DELETE FROM knowledge_claim_edges WHERE from_claim_id=?", (claim_id,))
    db.execute("DELETE FROM knowledge_claim_sources WHERE claim_id=?", (claim_id,))
    db.execute("DELETE FROM knowledge_claims_fts WHERE claim_id=?", (claim_id,))
    db.execute("DELETE FROM knowledge_claims WHERE id=?", (claim_id,))

    text = _claim_text(note)
    if not text:
        return
    db.execute(
        "INSERT INTO knowledge_claims VALUES (?,?,?,?,?,?,?,?,?)",
        (claim_id, note_id, text, kind, confidence, status, scope, stale_after, now),
    )
    for position, source in enumerate(note.metadata["sources"]):
        db.execute(
            "INSERT INTO knowledge_claim_sources(claim_id,position,source_anchor) VALUES (?,?,?)",
            (claim_id, position, str(source)),
        )
    if status == "active" and not note.stale:
        db.execute(
            "INSERT INTO knowledge_claims_fts(claim_id,text) VALUES (?,?)", (claim_id, text)
        )

    entities = note.metadata.get("entities", [])
    if not isinstance(entities, list):
        raise TypeError("entities must be a list")
    entity_ids: set[str] = set()
    for raw in entities:
        if isinstance(raw, str):
            raw = {"name": raw, "type": "entity"}
        if not isinstance(raw, dict) or not raw.get("name"):
            raise ValueError("each entity must be a name or mapping with name")
        entity_id = _id(str(raw.get("id") or raw["name"]))
        entity_ids.add(entity_id)
        db.execute(
            "INSERT INTO knowledge_entities(id,name,entity_type,scope,status,note_id) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name,entity_type=excluded.entity_type,"
            "scope=excluded.scope,status=excluded.status,note_id=excluded.note_id",
            (entity_id, str(raw["name"]), str(raw.get("type", "entity")), scope, status, note_id),
        )

    relations = note.metadata.get("relations", [])
    if not isinstance(relations, list):
        raise TypeError("relations must be a list")
    for position, relation in enumerate(relations):
        if not isinstance(relation, dict) or not {"subject", "predicate", "object"} <= relation.keys():
            raise ValueError("relations require subject, predicate, and object")
        subject = _id(str(relation["subject"]))
        object_ = _id(str(relation["object"]))
        if subject not in entity_ids or object_ not in entity_ids:
            raise ValueError("relation endpoints must be declared in entities")
        db.execute(
            "INSERT INTO knowledge_relations VALUES (?,?,?,?,?,?,?,?,?)",
            (
                f"{claim_id}:relation:{position}", subject, str(relation["predicate"]), object_,
                claim_id, note_id, scope, status, float(relation.get("confidence", confidence)),
            ),
        )

    replaces = note.metadata.get("replaces")
    if replaces:
        db.execute(
            "INSERT OR REPLACE INTO knowledge_claim_edges VALUES (?,?,?)",
            (claim_id, str(replaces), "supersedes"),
        )
    contradictions = note.metadata.get("contradicts", [])
    if isinstance(contradictions, str):
        contradictions = [contradictions]
    for target in contradictions:
        target = str(target)
        if not target.startswith("note:"):
            target = f"note:{target}"
        db.execute(
            "INSERT OR REPLACE INTO knowledge_claim_edges VALUES (?,?,?)",
            (claim_id, target, "contradicts"),
        )


def why(db: sqlite3.Connection, query: str) -> dict:
    """Return an active, fresh claim and its source/edge evidence path."""
    today = datetime.now(timezone.utc).date().isoformat()
    if query.startswith("note:"):
        row = db.execute(
            "SELECT * FROM knowledge_claims WHERE id=? AND status='active' "
            "AND (stale_after IS NULL OR stale_after>=?) LIMIT 1",
            (query, today),
        ).fetchone()
    else:
        row = db.execute(
            "SELECT c.* FROM knowledge_claims_fts f JOIN knowledge_claims c ON c.id=f.claim_id "
            "WHERE knowledge_claims_fts MATCH ? AND c.status='active' "
            "AND (c.stale_after IS NULL OR c.stale_after>=?) "
            "ORDER BY bm25(knowledge_claims_fts) LIMIT 1",
            (query, today),
        ).fetchone()
    if row is None:
        return {"found": False, "query": query, "reason": "no active, fresh claim in this scope"}
    claim = dict(row)
    sources = [r[0] for r in db.execute(
        "SELECT source_anchor FROM knowledge_claim_sources WHERE claim_id=? ORDER BY position",
        (claim["id"],),
    )]
    edges = [dict(r) for r in db.execute(
        "SELECT edge_type,from_claim_id,to_claim_id FROM knowledge_claim_edges "
        "WHERE from_claim_id=? OR to_claim_id=? ORDER BY edge_type,from_claim_id,to_claim_id",
        (claim["id"], claim["id"]),
    )]
    relations = [dict(r) for r in db.execute(
        "SELECT subject_entity_id,predicate,object_entity_id,confidence "
        "FROM knowledge_relations WHERE claim_id=? AND status='active' ORDER BY id",
        (claim["id"],),
    )]
    return {"found": True, "claim": claim, "sources": sources, "edges": edges, "relations": relations}
