"""Idempotent operational entry points for consolidation and watches."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .consolidation import daily_expiry_sweep, pollution_metrics, propose_weekly
from .proactivity import due_watches, fire_watch, value_gate


def run_due(db: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Fire every due watch once and drain the fail-closed value gate."""
    clock = now or datetime.now(timezone.utc)
    fired = []
    for watch in due_watches(db, clock.isoformat()):
        spec = json.loads(watch["spec_json"])
        event_id = fire_watch(
            db,
            watch["id"],
            {"trigger": "due", "spec": spec},
            spec.get("value"),
            clock,
            expected_next_fire_at=watch["next_fire_at"],
        )
        if event_id is not None:
            fired.append({"watch_id": watch["id"], "event_id": event_id})
    return {"fired": fired, "gate": value_gate(db)}


def run_maintenance(
    db: sqlite3.Connection, scope_dir: Path, weekly: bool = False, max_candidates: int = 100
) -> dict:
    result = {
        "expiry": daily_expiry_sweep(db, scope_dir),
        "watches": run_due(db),
        "pollution": pollution_metrics(db, scope_dir),
    }
    if weekly:
        path = propose_weekly(
            db, scope_dir, scope_dir / "changesets", max_candidates=max_candidates
        )
        result["weekly_changeset"] = str(path)
    return result
