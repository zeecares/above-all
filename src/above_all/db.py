from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

GLOBAL_MIGRATIONS = [
"""CREATE TABLE outcomes (id TEXT PRIMARY KEY, project TEXT, title TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','running','blocked','done','cancelled')), owner TEXT NOT NULL, source_anchor TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE sessions (id TEXT PRIMARY KEY, provider TEXT NOT NULL, project TEXT, mode TEXT NOT NULL, model TEXT, outcome_id TEXT, parent_session_id TEXT, started_at TEXT, ended_at TEXT, source_path TEXT, import_version TEXT, summary TEXT, tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL);
CREATE TABLE decisions (id TEXT PRIMARY KEY, ts TEXT NOT NULL, project TEXT, outcome_id TEXT, decision TEXT NOT NULL, rationale TEXT, source_anchor TEXT);
CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL, outcome_id TEXT, payload_json TEXT);
"""
]

PROJECT_MIGRATIONS = [
"""CREATE TABLE outcomes (id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','running','blocked','done','cancelled')), owner TEXT NOT NULL, source_anchor TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE sessions (id TEXT PRIMARY KEY, provider TEXT NOT NULL, mode TEXT NOT NULL, model TEXT, outcome_id TEXT, parent_session_id TEXT, started_at TEXT, ended_at TEXT, source_path TEXT, import_version TEXT, summary TEXT, tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL);
CREATE TABLE decisions (id TEXT PRIMARY KEY, ts TEXT NOT NULL, outcome_id TEXT, decision TEXT NOT NULL, rationale TEXT, source_anchor TEXT);
CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL, outcome_id TEXT, payload_json TEXT);
CREATE TABLE notes (id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, note_type TEXT NOT NULL, sources_json TEXT NOT NULL, generated TEXT NOT NULL, verified TEXT NOT NULL, status TEXT NOT NULL, stale_after TEXT, title TEXT NOT NULL, body TEXT NOT NULL, indexed_at TEXT NOT NULL);
CREATE VIRTUAL TABLE notes_fts USING fts5(note_id UNINDEXED, title, body, tokenize='porter unicode61');
"""
]


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def migrate(path: Path, migrations: Iterable[str]) -> sqlite3.Connection:
    db = connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    applied = {r[0] for r in db.execute("SELECT version FROM schema_migrations")}
    for version, sql in enumerate(migrations, 1):
        if version not in applied:
            with db:
                db.executescript(sql)
                db.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
    return db


def merge_rows(global_rows: Iterable[sqlite3.Row], project_rows: Iterable[sqlite3.Row]) -> list[dict]:
    merged = {row["id"]: dict(row) for row in global_rows}
    merged.update({row["id"]: dict(row) for row in project_rows})
    return [merged[key] for key in sorted(merged)]


# Weekend 4: schedules and internal proactive events belong in the control plane.
GLOBAL_MIGRATIONS.append("""CREATE TABLE watches (id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('clock','cadence','event','deadline')), spec_json TEXT NOT NULL, next_fire_at TEXT, interruption_policy TEXT NOT NULL DEFAULT 'value-gated', created_at TEXT NOT NULL);
CREATE TABLE proactive_events (id INTEGER PRIMARY KEY AUTOINCREMENT, watch_id TEXT, created_at TEXT NOT NULL, payload_json TEXT NOT NULL, value TEXT, status TEXT NOT NULL CHECK(status IN ('pending','surfaced','internal')) DEFAULT 'pending', FOREIGN KEY(watch_id) REFERENCES watches(id));
""")
PROJECT_MIGRATIONS.append("""CREATE TABLE watches (id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('clock','cadence','event','deadline')), spec_json TEXT NOT NULL, next_fire_at TEXT, interruption_policy TEXT NOT NULL DEFAULT 'value-gated', created_at TEXT NOT NULL);
CREATE TABLE proactive_events (id INTEGER PRIMARY KEY AUTOINCREMENT, watch_id TEXT, created_at TEXT NOT NULL, payload_json TEXT NOT NULL, value TEXT, status TEXT NOT NULL CHECK(status IN ('pending','surfaced','internal')) DEFAULT 'pending', FOREIGN KEY(watch_id) REFERENCES watches(id));
""")
