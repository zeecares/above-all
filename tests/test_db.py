import sqlite3

import pytest

from above_all.db import GLOBAL_MIGRATIONS, PROJECT_MIGRATIONS, merge_rows, migrate


def test_migrations_are_idempotent(tmp_path):
    path = tmp_path / "global.db"
    migrate(path, GLOBAL_MIGRATIONS).close()
    db = migrate(path, GLOBAL_MIGRATIONS)
    assert db.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == len(GLOBAL_MIGRATIONS)


def test_project_schema_has_notes_and_fts(tmp_path):
    db = migrate(tmp_path / "project.db", PROJECT_MIGRATIONS)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    assert {"outcomes", "sessions", "decisions", "events", "notes", "notes_fts"} <= tables


def test_project_overlays_global_on_id():
    a = sqlite3.connect(":memory:"); a.row_factory = sqlite3.Row
    b = sqlite3.connect(":memory:"); b.row_factory = sqlite3.Row
    for db in (a,b): db.execute("CREATE TABLE x(id TEXT, value TEXT)")
    a.execute("INSERT INTO x VALUES('same','global')"); b.execute("INSERT INTO x VALUES('same','project')")
    assert merge_rows(a.execute("SELECT * FROM x"), b.execute("SELECT * FROM x")) == [{"id":"same","value":"project"}]




# --- Migration forward-compat: historical database states must upgrade cleanly ---

# Snapshot of the weekend-4 era global migrations (v1 core, v2 watches), before
# the memory overlay added global notes. Kept as a literal so the test pins the
# historical on-disk state rather than the current constants.
_WEEKEND4_GLOBAL_V1 = """CREATE TABLE outcomes (id TEXT PRIMARY KEY, project TEXT, title TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','running','blocked','done','cancelled')), owner TEXT NOT NULL, source_anchor TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE sessions (id TEXT PRIMARY KEY, provider TEXT NOT NULL, project TEXT, mode TEXT NOT NULL, model TEXT, outcome_id TEXT, parent_session_id TEXT, started_at TEXT, ended_at TEXT, source_path TEXT, import_version TEXT, summary TEXT, tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL);
CREATE TABLE decisions (id TEXT PRIMARY KEY, ts TEXT NOT NULL, project TEXT, outcome_id TEXT, decision TEXT NOT NULL, rationale TEXT, source_anchor TEXT);
CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL, outcome_id TEXT, payload_json TEXT);
"""
_WEEKEND4_GLOBAL_V2_WATCHES = """CREATE TABLE watches (id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('clock','cadence','event','deadline')), spec_json TEXT NOT NULL, next_fire_at TEXT, interruption_policy TEXT NOT NULL DEFAULT 'value-gated', created_at TEXT NOT NULL, identity TEXT NOT NULL UNIQUE);
CREATE TABLE proactive_events (id INTEGER PRIMARY KEY AUTOINCREMENT, watch_id TEXT, created_at TEXT NOT NULL, payload_json TEXT NOT NULL, value TEXT, status TEXT NOT NULL CHECK(status IN ('pending','surfaced','internal')) DEFAULT 'pending', FOREIGN KEY(watch_id) REFERENCES watches(id));
"""
_OVERLAY_NOTES = """CREATE TABLE notes (id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, note_type TEXT NOT NULL, sources_json TEXT NOT NULL, generated TEXT NOT NULL, verified TEXT NOT NULL, status TEXT NOT NULL, stale_after TEXT, title TEXT NOT NULL, body TEXT NOT NULL, indexed_at TEXT NOT NULL);
CREATE VIRTUAL TABLE notes_fts USING fts5(note_id UNINDEXED, title, body, tokenize='porter unicode61');
"""


def _table_names(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}


def _applied_versions(db):
    return {r[0] for r in db.execute("SELECT version FROM schema_migrations")}


def test_weekend4_v2_global_db_migrates_forward(tmp_path):
    # A store created between weekend 4 and the memory overlay: v2 is watches.
    path = tmp_path / "global.db"
    migrate(path, [_WEEKEND4_GLOBAL_V1, _WEEKEND4_GLOBAL_V2_WATCHES]).close()
    seed = sqlite3.connect(path)
    seed.execute(
        "INSERT INTO watches (id, kind, spec_json, created_at, identity)"
        " VALUES ('w1', 'cadence', '{}', '2026-09-17T00:00:00+00:00', 'w1')"
    )
    seed.commit()
    seed.close()

    db = migrate(path, GLOBAL_MIGRATIONS)
    assert _applied_versions(db) == {1, 2, 3, 4}
    assert {"notes", "notes_fts", "watches", "proactive_events"} <= _table_names(db)
    assert db.execute("SELECT id FROM watches").fetchall()[0][0] == "w1"
    # Notes store is usable after the upgrade.
    db.execute(
        "INSERT INTO notes (id, path, note_type, sources_json, generated, verified,"
        " status, title, body, indexed_at) VALUES ('n1','/n1.md','fact','[]','','',"
        " 'active','t','b','2026-09-18T00:00:00+00:00')"
    )
    db.commit()
    db.close()


def test_overlay_window_global_db_migrates_forward(tmp_path):
    # A store created while the overlay had notes as v2 and watches as v3:
    # version 2 means notes, version 3 means watches. Under the restored order
    # both versions are already recorded, so nothing is re-applied or recreated.
    path = tmp_path / "global.db"
    migrate(path, [_WEEKEND4_GLOBAL_V1, _OVERLAY_NOTES, _WEEKEND4_GLOBAL_V2_WATCHES]).close()
    seed = sqlite3.connect(path)
    seed.execute(
        "INSERT INTO watches (id, kind, spec_json, created_at, identity)"
        " VALUES ('w9', 'clock', '{}', '2026-09-18T00:00:00+00:00', 'w9')"
    )
    seed.execute(
        "INSERT INTO notes (id, path, note_type, sources_json, generated, verified,"
        " status, title, body, indexed_at) VALUES ('n9','/n9.md','fact','[]','','',"
        " 'active','t','b','2026-09-18T00:00:00+00:00')"
    )
    seed.commit()
    seed.close()

    db = migrate(path, GLOBAL_MIGRATIONS)
    assert _applied_versions(db) == {1, 2, 3, 4}
    assert {"notes", "notes_fts", "watches", "proactive_events"} <= _table_names(db)
    assert db.execute("SELECT id FROM watches").fetchall()[0][0] == "w9"
    assert db.execute("SELECT id FROM notes").fetchall()[0][0] == "n9"
    db.close()


def test_legacy_untracked_global_db_is_adopted(tmp_path):
    # A store from before schema_migrations tracking: tables exist, no version
    # rows. IF NOT EXISTS DDL lets the runner adopt it instead of crashing.
    path = tmp_path / "global.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(_WEEKEND4_GLOBAL_V1)
    legacy.executescript(_WEEKEND4_GLOBAL_V2_WATCHES)
    legacy.executescript(_OVERLAY_NOTES)
    legacy.commit()
    legacy.close()
    assert sqlite3.connect(path).execute(
        "SELECT name FROM sqlite_master WHERE name='schema_migrations'"
    ).fetchall() == []

    db = migrate(path, GLOBAL_MIGRATIONS)
    assert _applied_versions(db) == {1, 2, 3, 4}
    assert {"outcomes", "watches", "notes", "notes_fts"} <= _table_names(db)
    db.close()


def test_global_and_project_orders_stay_fixed():
    # Guard against renumbering: watches must remain global v2 and notes v3.
    assert "CREATE TABLE IF NOT EXISTS watches" in GLOBAL_MIGRATIONS[1]
    assert "CREATE TABLE IF NOT EXISTS notes" in GLOBAL_MIGRATIONS[2]
    assert "CREATE TABLE IF NOT EXISTS notes" in PROJECT_MIGRATIONS[0]
    assert "CREATE TABLE IF NOT EXISTS watches" in PROJECT_MIGRATIONS[1]
    assert "knowledge_claims" in GLOBAL_MIGRATIONS[3]
    assert "knowledge_claims" in PROJECT_MIGRATIONS[2]



def test_each_migration_and_marker_commit_atomically(tmp_path):
    path = tmp_path / "atomic.db"
    broken = [
        (
            "CREATE TABLE durable (id INTEGER PRIMARY KEY);"
            "INSERT INTO missing_table VALUES (1);"
        )
    ]
    with pytest.raises(sqlite3.OperationalError):
        migrate(path, broken)
    db = sqlite3.connect(path)
    assert "durable" not in _table_names(db)
    assert _applied_versions(db) == set()
    db.close()
