"""Persisted watches and a fail-closed restraint gate."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from .routing import classify_value

ALLOWED_VALUE_CATEGORIES = {"decision", "risk", "saved_step"}
ValueClassifier = Callable[[str, dict], dict | None]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _identity(kind: str, spec: dict) -> str:
    return hashlib.sha256(
        json.dumps({"kind": kind, "spec": spec}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def create_watch(
    db: sqlite3.Connection,
    kind: str,
    spec: dict,
    next_fire_at: str | None = None,
    policy: str = "value-gated",
) -> str:
    if kind not in {"clock", "cadence", "event", "deadline"}:
        raise ValueError(f"unsupported watch kind {kind!r}")
    if policy not in {"value-gated", "internal-only"}:
        raise ValueError(f"unsupported interruption policy {policy!r}")
    if kind == "cadence" and (not isinstance(spec.get("minutes"), int) or spec["minutes"] <= 0):
        raise ValueError("cadence watch requires positive integer minutes")
    identity = _identity(kind, spec)
    with db:
        db.execute(
            "INSERT INTO watches(id,kind,spec_json,next_fire_at,interruption_policy,created_at,identity) VALUES (?,?,?,?,?,?,?) ON CONFLICT(identity) DO UPDATE SET next_fire_at=COALESCE(excluded.next_fire_at,watches.next_fire_at), interruption_policy=excluded.interruption_policy",
            (
                identity,
                kind,
                json.dumps(spec, sort_keys=True),
                next_fire_at,
                policy,
                _now(),
                identity,
            ),
        )
    return identity


def due_watches(db: sqlite3.Connection, now: str | None = None) -> list[dict]:
    when = now or _now()
    return [
        dict(row)
        for row in db.execute(
            "SELECT * FROM watches WHERE next_fire_at IS NOT NULL AND next_fire_at <= ? ORDER BY next_fire_at,id",
            (when,),
        )
    ]


def fire_watch(
    db: sqlite3.Connection,
    watch_id: str,
    payload: dict,
    value: str | None = None,
    now: datetime | None = None,
    *,
    expected_next_fire_at: str | None = None,
) -> int | None:
    clock = now or datetime.now(timezone.utc)
    with db:
        watch = db.execute(
            "SELECT kind,spec_json,next_fire_at FROM watches WHERE id=?", (watch_id,)
        ).fetchone()
        if not watch:
            raise ValueError(f"unknown watch {watch_id!r}")
        if expected_next_fire_at is not None and watch[2] != expected_next_fire_at:
            return None
        if watch[0] == "cadence":
            minutes = json.loads(watch[1])["minutes"]
            # Advance from the scheduled occurrence, not wall-clock completion, so
            # DST/slow maintenance cannot drift the cadence. Catch up past `clock`
            # in one transaction and emit this occurrence once.
            scheduled = datetime.fromisoformat(watch[2])
            next_fire = scheduled
            while next_fire <= clock:
                next_fire += timedelta(minutes=minutes)
            replacement = next_fire.isoformat()
        else:
            replacement = None
        if expected_next_fire_at is not None:
            changed = db.execute(
                "UPDATE watches SET next_fire_at=? WHERE id=? AND next_fire_at=?",
                (replacement, watch_id, expected_next_fire_at),
            )
            if changed.rowcount != 1:
                return None
        else:
            db.execute("UPDATE watches SET next_fire_at=? WHERE id=?", (replacement, watch_id))
        cursor = db.execute(
            "INSERT INTO proactive_events(watch_id,created_at,payload_json,value) VALUES (?,?,?,?)",
            (
                watch_id,
                clock.isoformat(),
                json.dumps(payload, sort_keys=True),
                value.strip() if value and value.strip() else None,
            ),
        )
    return cursor.lastrowid


def value_gate(db: sqlite3.Connection, classifier: ValueClassifier | None = None) -> dict:
    """Surface only classifier-confirmed decision/risk/saved-step value; otherwise log internally."""
    classifier = classifier or classify_value
    surfaced, internal = [], []
    rows = db.execute(
        "SELECT e.id,e.watch_id,e.payload_json,e.value,w.interruption_policy FROM proactive_events e JOIN watches w ON w.id=e.watch_id WHERE e.status='pending' ORDER BY e.id"
    ).fetchall()
    with db:
        for row in rows:
            item = {
                "id": row[0],
                "watch_id": row[1],
                "payload": json.loads(row[2]),
                "value": row[3],
            }
            decision = None
            if row[4] == "value-gated" and row[3]:
                try:
                    decision = classifier(row[3], item["payload"])
                except (TypeError, ValueError, RuntimeError):
                    decision = None
            valid = (
                isinstance(decision, dict)
                and decision.get("category") in ALLOWED_VALUE_CATEGORIES
                and isinstance(decision.get("reason"), str)
                and bool(decision["reason"].strip())
            )
            if valid:
                item["value_category"] = decision["category"]
                item["value_reason"] = decision["reason"].strip()
                db.execute("UPDATE proactive_events SET status='surfaced' WHERE id=?", (row[0],))
                surfaced.append(item)
            else:
                db.execute("UPDATE proactive_events SET status='internal' WHERE id=?", (row[0],))
                internal.append(item)
    return {"surfaced": surfaced, "internal_count": len(internal)}
