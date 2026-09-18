"""Bounded, review-gated memory consolidation."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4


def _claim(body: str) -> str:
    lines = body.strip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).strip().casefold()

from .notes import approve_candidate, index_note, parse_note, set_note_status


def daily_expiry_sweep(db: sqlite3.Connection, scope_dir: Path, today: date | None = None) -> dict:
    """Mark expired active notes needs-review and remove them from FTS. Candidates are untouched."""
    cutoff = (today or datetime.now(timezone.utc).date()).isoformat()
    rows = db.execute(
        "SELECT id,path FROM notes WHERE status='active' AND stale_after IS NOT NULL AND stale_after < ?",
        (cutoff,),
    ).fetchall()
    expired = []
    for note_id, raw_path in rows:
        path = Path(raw_path)
        set_note_status(path, "needs-review")
        index_note(db, path)
        expired.append(note_id)
    return {"expired": sorted(expired), "candidates_touched": 0}


def _candidate_records(scope_dir: Path) -> list[dict]:
    records = []
    for path in sorted((scope_dir / "candidates").glob("*.md")):
        try:
            note = parse_note(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        records.append({"id": path.stem, "path": path, "note": note})
    return records


def propose_weekly(
    db: sqlite3.Connection,
    scope_dir: Path,
    output_dir: Path,
    analysis_dir: Path | None = None,
    max_candidates: int = 100,
) -> Path:
    """Write a bounded changeset. This function never applies memory changes."""
    active = db.execute("SELECT id,title,body,path FROM notes WHERE status='active'").fetchall()
    active_by_body = {_claim(row[2]): row for row in active}
    active_tokens = {row[0]: set(_claim(row[2]).split()) for row in active}
    changes = []
    for record in _candidate_records(scope_dir)[:max_candidates]:
        note = record["note"]
        status = note.metadata["status"]
        if status not in {"candidate", "reviewed"}:
            continue
        normalized = _claim(note.body)
        if normalized in active_by_body:
            changes.append({"action": "reject_duplicate", "candidate_id": record["id"], "active_id": active_by_body[normalized][0]})
            continue
        words = set(normalized.split())
        overlap = [note_id for note_id, tokens in active_tokens.items() if tokens and len(words & tokens) / max(1, min(len(words), len(tokens))) >= 0.6]
        contradiction = bool(overlap) and any(token in words for token in ("not", "never", "instead", "changed"))
        if contradiction:
            changes.append({"action": "flag_contradiction", "candidate_id": record["id"], "active_ids": sorted(overlap)})
        elif status == "reviewed":
            changes.append({"action": "promote", "candidate_id": record["id"]})
        else:
            changes.append({"action": "await_review", "candidate_id": record["id"]})
    trace_inputs = []
    if analysis_dir and analysis_dir.is_dir():
        for path in sorted(analysis_dir.glob("*-candidates.json")):
            trace_inputs.append({"path": str(path), "sha256": __import__("hashlib").sha256(path.read_bytes()).hexdigest()})
    metrics = pollution_metrics(db, scope_dir)
    payload = {
        "id": uuid4().hex,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "proposed",
        "bounded_candidates": max_candidates,
        "changes": changes,
        "trace_analysis_inputs": trace_inputs,
        "pollution": metrics,
        "requires_explicit_approve": True,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"changeset-{payload['id']}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def apply_changeset(db: sqlite3.Connection, scope_dir: Path, path: Path, approve: bool = False) -> dict:
    if not approve:
        raise ValueError("explicit approve is required to apply a consolidation changeset")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("status") != "proposed":
        raise ValueError("changeset is not proposed")
    applied = []
    for change in payload["changes"]:
        candidate_id = change["candidate_id"]
        candidate = scope_dir / "candidates" / f"{candidate_id}.md"
        if change["action"] == "promote":
            set_note_status(candidate, "candidate")
            approve_candidate(db, scope_dir, candidate_id)
            applied.append(change)
        elif change["action"] == "reject_duplicate":
            set_note_status(candidate, "rejected", {"duplicates": f"note:{change['active_id']}"})
            applied.append(change)
        elif change["action"] == "flag_contradiction":
            set_note_status(candidate, "needs-review", {"contradicts": [f"note:{x}" for x in change["active_ids"]]})
            applied.append(change)
    payload["status"] = "applied"
    payload["applied_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"status": "applied", "changes": applied}


def pollution_metrics(db: sqlite3.Connection, scope_dir: Path) -> dict:
    statuses = dict(db.execute("SELECT status,COUNT(*) FROM notes GROUP BY status").fetchall())
    candidates = len(list((scope_dir / "candidates").glob("*.md"))) if (scope_dir / "candidates").is_dir() else 0
    context = scope_dir / "AGENT_CONTEXT.md"
    sessions = db.execute("SELECT COALESCE(SUM(tokens_in),0),COALESCE(SUM(tokens_out),0),COALESCE(SUM(cost_usd),0),COUNT(*) FROM sessions").fetchone()
    return {
        "notes_by_status": statuses,
        "candidate_files": candidates,
        "context_bytes": context.stat().st_size if context.is_file() else 0,
        "tokens_in": sessions[0], "tokens_out": sessions[1], "cost_usd": sessions[2], "sessions": sessions[3],
    }
