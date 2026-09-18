"""Persisted watches and one restraint gate for every proactive event."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_watch(db: sqlite3.Connection, kind: str, spec: dict, next_fire_at: str | None = None, policy: str = "value-gated") -> str:
    if kind not in {"clock", "cadence", "event", "deadline"}:
        raise ValueError(f"unsupported watch kind {kind!r}")
    if kind == "cadence" and (not isinstance(spec.get("minutes"), int) or spec["minutes"] <= 0):
        raise ValueError("cadence watch requires positive integer minutes")
    watch_id = uuid4().hex
    with db:
        db.execute("INSERT INTO watches VALUES (?,?,?,?,?,?)", (watch_id, kind, json.dumps(spec, sort_keys=True), next_fire_at, policy, _now()))
    return watch_id


def due_watches(db: sqlite3.Connection, now: str | None = None) -> list[dict]:
    when = now or _now()
    return [dict(row) for row in db.execute("SELECT * FROM watches WHERE next_fire_at IS NOT NULL AND next_fire_at <= ? ORDER BY next_fire_at,id", (when,))]


def fire_watch(db: sqlite3.Connection, watch_id: str, payload: dict, value: str | None = None, now: datetime | None = None) -> int:
    clock = now or datetime.now(timezone.utc)
    watch = db.execute("SELECT kind,spec_json FROM watches WHERE id=?", (watch_id,)).fetchone()
    if not watch:
        raise ValueError(f"unknown watch {watch_id!r}")
    with db:
        cursor = db.execute("INSERT INTO proactive_events(watch_id,created_at,payload_json,value) VALUES (?,?,?,?)", (watch_id, clock.isoformat(), json.dumps(payload, sort_keys=True), value.strip() if value and value.strip() else None))
        if watch[0] == "cadence":
            minutes = json.loads(watch[1])["minutes"]
            db.execute("UPDATE watches SET next_fire_at=? WHERE id=?", ((clock + timedelta(minutes=minutes)).isoformat(), watch_id))
        else:
            db.execute("UPDATE watches SET next_fire_at=NULL WHERE id=?", (watch_id,))
    return cursor.lastrowid


def value_gate(db: sqlite3.Connection) -> dict:
    """Surface only events that name a concrete value. Keep the rest internal and logged."""
    surfaced, internal = [], []
    rows = db.execute("SELECT id,watch_id,payload_json,value FROM proactive_events WHERE status='pending' ORDER BY id").fetchall()
    with db:
        for row in rows:
            item = {"id": row[0], "watch_id": row[1], "payload": json.loads(row[2]), "value": row[3]}
            if row[3]:
                db.execute("UPDATE proactive_events SET status='surfaced' WHERE id=?", (row[0],))
                surfaced.append(item)
            else:
                db.execute("UPDATE proactive_events SET status='internal' WHERE id=?", (row[0],))
                internal.append(item)
    return {"surfaced": surfaced, "internal_count": len(internal)}
