from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

# Migration order is append-only history: a version number, once shipped, must
# keep referring to the same schema change forever. Existing databases record
# which versions they applied, so inserting a new migration in the middle
# renumbers every later one and corrupts upgrades (a store that applied
# "watches" as v2 would skip the new v2 and then crash re-creating watches as
# v3). New schema changes always go at the END of the list.
#
# Global history: v1 core tables, v2 watches (weekend 4), v3 global notes
# (memory overlay). Project history: v1 core+notes, v2 watches.
#
# All DDL uses IF NOT EXISTS so that stores created before schema_migrations
# tracking existed can be adopted without manual repair: the script runs,
# finds its tables already present, and simply records the version.

GLOBAL_MIGRATIONS = [
"""CREATE TABLE IF NOT EXISTS outcomes (id TEXT PRIMARY KEY, project TEXT, title TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','running','blocked','done','cancelled')), owner TEXT NOT NULL, source_anchor TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, provider TEXT NOT NULL, project TEXT, mode TEXT NOT NULL, model TEXT, outcome_id TEXT, parent_session_id TEXT, started_at TEXT, ended_at TEXT, source_path TEXT, import_version TEXT, summary TEXT, tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL);
CREATE TABLE IF NOT EXISTS decisions (id TEXT PRIMARY KEY, ts TEXT NOT NULL, project TEXT, outcome_id TEXT, decision TEXT NOT NULL, rationale TEXT, source_anchor TEXT);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL, outcome_id TEXT, payload_json TEXT);
"""
]

PROJECT_MIGRATIONS = [
"""CREATE TABLE IF NOT EXISTS outcomes (id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','running','blocked','done','cancelled')), owner TEXT NOT NULL, source_anchor TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, provider TEXT NOT NULL, mode TEXT NOT NULL, model TEXT, outcome_id TEXT, parent_session_id TEXT, started_at TEXT, ended_at TEXT, source_path TEXT, import_version TEXT, summary TEXT, tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL);
CREATE TABLE IF NOT EXISTS decisions (id TEXT PRIMARY KEY, ts TEXT NOT NULL, outcome_id TEXT, decision TEXT NOT NULL, rationale TEXT, source_anchor TEXT);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL, outcome_id TEXT, payload_json TEXT);
CREATE TABLE IF NOT EXISTS notes (id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, note_type TEXT NOT NULL, sources_json TEXT NOT NULL, generated TEXT NOT NULL, verified TEXT NOT NULL, status TEXT NOT NULL, stale_after TEXT, title TEXT NOT NULL, body TEXT NOT NULL, indexed_at TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(note_id UNINDEXED, title, body, tokenize='porter unicode61');
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
            # executescript() commits any pending transaction before running, so
            # wrap the migration and version marker in one explicit script. Without
            # this, a crash after DDL but before the marker leaves a half-recorded
            # migration that cannot be distinguished from an old store.
            quoted_version = int(version)
            db.executescript(
                "BEGIN IMMEDIATE;\n"
                + sql
                + f"\nINSERT INTO schema_migrations(version) VALUES ({quoted_version});\n"
                + "COMMIT;"
            )
    return db


def merge_rows(global_rows: Iterable[sqlite3.Row], project_rows: Iterable[sqlite3.Row]) -> list[dict]:
    merged = {row["id"]: dict(row) for row in global_rows}
    merged.update({row["id"]: dict(row) for row in project_rows})
    return [merged[key] for key in sorted(merged)]


# Weekend 4: schedules and internal proactive events belong in the control plane.
GLOBAL_MIGRATIONS.append("""CREATE TABLE IF NOT EXISTS watches (id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('clock','cadence','event','deadline')), spec_json TEXT NOT NULL, next_fire_at TEXT, interruption_policy TEXT NOT NULL DEFAULT 'value-gated', created_at TEXT NOT NULL, identity TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS proactive_events (id INTEGER PRIMARY KEY AUTOINCREMENT, watch_id TEXT, created_at TEXT NOT NULL, payload_json TEXT NOT NULL, value TEXT, status TEXT NOT NULL CHECK(status IN ('pending','surfaced','internal')) DEFAULT 'pending', FOREIGN KEY(watch_id) REFERENCES watches(id));
""")
PROJECT_MIGRATIONS.append("""CREATE TABLE IF NOT EXISTS watches (id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('clock','cadence','event','deadline')), spec_json TEXT NOT NULL, next_fire_at TEXT, interruption_policy TEXT NOT NULL DEFAULT 'value-gated', created_at TEXT NOT NULL, identity TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS proactive_events (id INTEGER PRIMARY KEY AUTOINCREMENT, watch_id TEXT, created_at TEXT NOT NULL, payload_json TEXT NOT NULL, value TEXT, status TEXT NOT NULL CHECK(status IN ('pending','surfaced','internal')) DEFAULT 'pending', FOREIGN KEY(watch_id) REFERENCES watches(id));
""")

# Memory overlay: project knowledge merges over a global notes store.
GLOBAL_MIGRATIONS.append("""CREATE TABLE IF NOT EXISTS notes (id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, note_type TEXT NOT NULL, sources_json TEXT NOT NULL, generated TEXT NOT NULL, verified TEXT NOT NULL, status TEXT NOT NULL, stale_after TEXT, title TEXT NOT NULL, body TEXT NOT NULL, indexed_at TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(note_id UNINDEXED, title, body, tokenize='porter unicode61');
""")



# Provenance-backed knowledge map. It mirrors reviewed notes only; candidate files are never indexed.
_KNOWLEDGE_MAP = """CREATE TABLE IF NOT EXISTS knowledge_entities (id TEXT PRIMARY KEY, name TEXT NOT NULL, entity_type TEXT NOT NULL, scope TEXT NOT NULL CHECK(scope IN ('global','project')), status TEXT NOT NULL, note_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS knowledge_claims (id TEXT PRIMARY KEY, note_id TEXT NOT NULL UNIQUE, text TEXT NOT NULL, claim_kind TEXT NOT NULL CHECK(claim_kind IN ('premise','inference')), confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1), status TEXT NOT NULL, scope TEXT NOT NULL CHECK(scope IN ('global','project')), stale_after TEXT, indexed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS knowledge_claim_sources (claim_id TEXT NOT NULL, position INTEGER NOT NULL, source_anchor TEXT NOT NULL, PRIMARY KEY(claim_id,position), FOREIGN KEY(claim_id) REFERENCES knowledge_claims(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS knowledge_relations (id TEXT PRIMARY KEY, subject_entity_id TEXT NOT NULL, predicate TEXT NOT NULL, object_entity_id TEXT NOT NULL, claim_id TEXT NOT NULL, note_id TEXT NOT NULL, scope TEXT NOT NULL CHECK(scope IN ('global','project')), status TEXT NOT NULL, confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1), FOREIGN KEY(claim_id) REFERENCES knowledge_claims(id) ON DELETE CASCADE, FOREIGN KEY(subject_entity_id) REFERENCES knowledge_entities(id), FOREIGN KEY(object_entity_id) REFERENCES knowledge_entities(id));
CREATE TABLE IF NOT EXISTS knowledge_claim_edges (from_claim_id TEXT NOT NULL, to_claim_id TEXT NOT NULL, edge_type TEXT NOT NULL CHECK(edge_type IN ('supersedes','contradicts')), PRIMARY KEY(from_claim_id,to_claim_id,edge_type));
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_claims_fts USING fts5(claim_id UNINDEXED, text, tokenize='porter unicode61');
"""
GLOBAL_MIGRATIONS.append(_KNOWLEDGE_MAP)
PROJECT_MIGRATIONS.append(_KNOWLEDGE_MAP)
