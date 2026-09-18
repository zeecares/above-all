"""Bounded, review-gated memory consolidation."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from .notes import approve_candidate, index_note, parse_note, set_note_status

ALLOWED_ACTIONS = {"reject_duplicate", "flag_contradiction", "promote", "replace", "await_review", "trace_candidate"}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _claim(body: str) -> str:
    lines = body.strip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).strip().casefold()


def _active(db: sqlite3.Connection) -> list:
    return db.execute("SELECT id,title,body,path FROM notes WHERE status='active' ORDER BY id").fetchall()


def _classify(note, active: list) -> dict:
    normalized = _claim(note.body)
    exact = [row[0] for row in active if _claim(row[2]) == normalized]
    if exact:
        return {"action": "reject_duplicate", "active_id": exact[0]}
    words = set(normalized.split())
    overlap = [row[0] for row in active if (tokens := set(_claim(row[2]).split())) and len(words & tokens) / max(1, min(len(words), len(tokens))) >= 0.6]
    contradiction = bool(overlap) and any(token in words for token in ("not", "never", "instead", "changed", "no"))
    if contradiction:
        if note.metadata["status"] == "reviewed" and len(overlap) == 1:
            return {"action": "replace", "active_id": overlap[0]}
        return {"action": "flag_contradiction", "active_ids": sorted(overlap)}
    if note.metadata["status"] == "reviewed":
        return {"action": "promote"}
    return {"action": "await_review"}


def daily_expiry_sweep(db: sqlite3.Connection, scope_dir: Path, today: date | None = None) -> dict:
    """Mark expired active notes needs-review and remove them from FTS."""
    cutoff = (today or datetime.now(timezone.utc).date()).isoformat()
    rows = db.execute("SELECT id,path FROM notes WHERE status='active' AND stale_after IS NOT NULL AND stale_after < ?", (cutoff,)).fetchall()
    # Validate every source before the first write so a broken index cannot cause a partial sweep.
    paths = [(note_id, Path(raw_path)) for note_id, raw_path in rows]
    for note_id, path in paths:
        if not path.is_file():
            raise FileNotFoundError(f"indexed active note {note_id!r} is missing: {path}")
        parse_note(path.read_text(encoding="utf-8"))
    expired = []
    for note_id, path in paths:
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


def _trace_inputs(analysis_dir: Path | None, limit: int) -> tuple[list[dict], list[dict]]:
    sources, changes = [], []
    if not analysis_dir or not analysis_dir.is_dir():
        return sources, changes
    remaining = limit
    for path in sorted(analysis_dir.glob("*-candidates.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"malformed trace queue {path}: {exc}") from exc
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise ValueError(f"malformed trace queue {path}: expected a list of objects")
        sources.append({"path": str(path), "sha256": _sha(path), "items": len(data)})
        for offset, item in enumerate(data[:remaining]):
            changes.append({"action": "trace_candidate", "queue": path.stem, "index": offset, "proposal": item, "source_sha256": _sha(path)})
        remaining -= min(remaining, len(data))
        if remaining == 0:
            break
    return sources, changes


def propose_weekly(db: sqlite3.Connection, scope_dir: Path, output_dir: Path, analysis_dir: Path | None = None, max_candidates: int = 100) -> Path:
    """Write a bounded, hash-bound proposal; never apply memory or routing changes."""
    if max_candidates < 0:
        raise ValueError("max_candidates must be non-negative")
    active = _active(db)
    changes = []
    for record in _candidate_records(scope_dir)[:max_candidates]:
        note = record["note"]
        if note.metadata["status"] not in {"candidate", "reviewed"}:
            continue
        change = {"candidate_id": record["id"], "candidate_sha256": _sha(record["path"]), **_classify(note, active)}
        if "active_id" in change:
            active_path = Path(next(row[3] for row in active if row[0] == change["active_id"]))
            change["active_sha256"] = _sha(active_path)
        if "active_ids" in change:
            change["active_snapshots"] = {row[0]: _sha(Path(row[3])) for row in active if row[0] in change["active_ids"]}
        changes.append(change)
    trace_inputs, trace_changes = _trace_inputs(analysis_dir, max(0, max_candidates - len(changes)))
    changes.extend(trace_changes)
    payload = {"id": uuid4().hex, "created_at": datetime.now(timezone.utc).isoformat(), "status": "proposed", "bounded_candidates": max_candidates, "changes": changes, "trace_analysis_inputs": trace_inputs, "pollution": pollution_metrics(db, scope_dir), "requires_explicit_approve": True}
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"changeset-{payload['id']}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _validate(payload: dict, scope_dir: Path) -> list[dict]:
    if payload.get("status") == "applied":
        return []
    if payload.get("status") != "proposed" or payload.get("requires_explicit_approve") is not True or not isinstance(payload.get("changes"), list):
        raise ValueError("invalid or non-proposed changeset")
    memory = []
    for change in payload["changes"]:
        if not isinstance(change, dict) or change.get("action") not in ALLOWED_ACTIONS:
            raise ValueError(f"unknown or malformed changeset action: {change!r}")
        if change["action"] == "trace_candidate":
            if not isinstance(change.get("proposal"), dict):
                raise ValueError("malformed trace candidate")
            continue
        cid = change.get("candidate_id")
        candidate = scope_dir / "candidates" / f"{cid}.md"
        if not candidate.is_file() or _sha(candidate) != change.get("candidate_sha256"):
            raise ValueError(f"candidate snapshot changed or disappeared: {cid!r}")
        parse_note(candidate.read_text(encoding="utf-8"))
        memory.append(change)
    return memory


def apply_changeset(db: sqlite3.Connection, scope_dir: Path, path: Path, approve: bool = False) -> dict:
    if not approve:
        raise ValueError("explicit approve is required to apply a consolidation changeset")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("status") == "applied":
        return {"status": "applied", "changes": payload.get("applied_changes", []), "idempotent": True}
    memory = _validate(payload, scope_dir)
    active = _active(db)
    drifted = []
    for change in memory:
        candidate = scope_dir / "candidates" / f"{change['candidate_id']}.md"
        current = _classify(parse_note(candidate.read_text(encoding="utf-8")), active)
        planned = {k: change[k] for k in ("action", "active_id", "active_ids") if k in change}
        if current != planned:
            drifted.append(change["candidate_id"])
        if change.get("active_id"):
            rows = [row for row in active if row[0] == change["active_id"]]
            if not rows or _sha(Path(rows[0][3])) != change.get("active_sha256"):
                drifted.append(change["candidate_id"])
        if change.get("active_ids"):
            hashes = {row[0]: _sha(Path(row[3])) for row in active if row[0] in change["active_ids"]}
            if hashes != change.get("active_snapshots"):
                drifted.append(change["candidate_id"])
    if drifted:
        for cid in sorted(set(drifted)):
            set_note_status(scope_dir / "candidates" / f"{cid}.md", "needs-review", {"review_reason": "active-memory drift after proposal"})
        payload.update(status="drifted", drifted_candidates=sorted(set(drifted)), failed_at=datetime.now(timezone.utc).isoformat())
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return {"status": "drifted", "changes": [], "drifted_candidates": sorted(set(drifted))}

    # All references/actions are valid before the first write. Keep byte backups for loud rollback.
    touched = {scope_dir / "candidates" / f"{c['candidate_id']}.md" for c in memory}
    for c in memory:
        if c.get("active_id"):
            touched.add(scope_dir / "notes" / f"{c['active_id']}.md")
        touched.add(scope_dir / "notes" / f"{c['candidate_id']}.md")
    backups = {p: p.read_bytes() if p.exists() else None for p in touched}
    applied = []
    try:
        for change in memory:
            cid = change["candidate_id"]
            candidate = scope_dir / "candidates" / f"{cid}.md"
            action = change["action"]
            if action == "promote":
                set_note_status(candidate, "candidate"); approve_candidate(db, scope_dir, cid); applied.append(change)
            elif action == "replace":
                set_note_status(candidate, "candidate"); approve_candidate(db, scope_dir, cid, replaces=change["active_id"]); applied.append(change)
            elif action == "reject_duplicate":
                set_note_status(candidate, "rejected", {"duplicates": f"note:{change['active_id']}"}); applied.append(change)
            elif action == "flag_contradiction":
                set_note_status(candidate, "needs-review", {"contradicts": [f"note:{x}" for x in change["active_ids"]]}); applied.append(change)
        payload.update(status="applied", applied_at=datetime.now(timezone.utc).isoformat(), applied_changes=applied)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except Exception as exc:
        for target, data in backups.items():
            if data is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)
        # Rebuild affected index entries from restored note files.
        for target in backups:
            if target.parent == scope_dir / "notes" and target.exists():
                index_note(db, target)
        payload.update(status="failed", failed_at=datetime.now(timezone.utc).isoformat(), error=str(exc))
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        raise RuntimeError(f"changeset failed and was rolled back: {exc}") from exc
    return {"status": "applied", "changes": applied}


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _event_count(db: sqlite3.Connection, kinds: tuple[str, ...]) -> int:
    if not _table_exists(db, "events"):
        return 0
    marks = ",".join("?" for _ in kinds)
    return db.execute(f"SELECT COUNT(*) FROM events WHERE kind IN ({marks})", kinds).fetchone()[0]


def pollution_metrics(db: sqlite3.Connection, scope_dir: Path) -> dict:
    statuses = dict(db.execute("SELECT status,COUNT(*) FROM notes GROUP BY status").fetchall()) if _table_exists(db, "notes") else {}
    sessions = db.execute("SELECT COALESCE(SUM(tokens_in),0),COALESCE(SUM(tokens_out),0),COALESCE(SUM(cost_usd),0),COUNT(*),SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) FROM sessions LEFT JOIN outcomes ON sessions.outcome_id=outcomes.id").fetchone()
    retrievals = _event_count(db, ("note_retrieved",))
    ignored = _event_count(db, ("retrieval_ignored", "retrieval_corrected"))
    failures = _event_count(db, ("candidate_creation_failed", "extraction_failure", "empty_extraction_batch"))
    if _table_exists(db, "trace_imports"):
        failures += db.execute("SELECT COUNT(*) FROM trace_imports WHERE status='failed'").fetchone()[0]
    context = scope_dir / "AGENT_CONTEXT.md"
    return {
        "notes_by_status": statuses,
        "admission": {"candidates_created": _event_count(db, ("candidate_created",)), "approved": _event_count(db, ("candidate_approved",)), "later_retrieved": retrievals},
        "recent_replacements_or_contradictions": _event_count(db, ("note_replaced", "note_contradiction")),
        "retrieval_recall": {"retrieved": retrievals, "ignored_or_corrected": ignored, "rate": None if retrievals == 0 else (retrievals - ignored) / retrievals},
        "extraction": {"failures_or_empty_batches": failures, "visible": True},
        "candidate_files": len(list((scope_dir / "candidates").glob("*.md"))) if (scope_dir / "candidates").is_dir() else 0,
        "context_bytes": context.stat().st_size if context.is_file() else 0,
        "prompt_memory_tokens_per_completed_outcome": None if not sessions[4] else sessions[0] / sessions[4],
        "tokens_in": sessions[0], "tokens_out": sessions[1], "cost_usd": sessions[2], "sessions": sessions[3],
    }
