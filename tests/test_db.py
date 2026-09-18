import sqlite3

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

