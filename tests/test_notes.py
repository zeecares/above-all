from datetime import datetime, timedelta, timezone

import pytest

from above_all.db import PROJECT_MIGRATIONS, migrate
from above_all.notes import index_note, parse_note, search_notes


def write_note(path, status="active", stale_after=None, title="Useful fact"):
    stale = "null" if stale_after is None else stale_after
    path.write_text(f"---\ntype: fact\nsources: [file:test]\ngenerated: 2026-09-17T00:00:00Z\nverified: source\nstatus: {status}\nstale_after: {stale}\n---\n# {title}\n\nalpha beta\n")


def test_header_is_required():
    with pytest.raises(ValueError): parse_note("hello")


def test_active_fresh_note_is_searchable(tmp_path):
    db=migrate(tmp_path/"db", PROJECT_MIGRATIONS); p=tmp_path/"n.md"; write_note(p)
    index_note(db,p)
    assert search_notes(db,"alpha")[0]["title"] == "Useful fact"


def test_stale_note_is_not_searchable(tmp_path):
    db=migrate(tmp_path/"db", PROJECT_MIGRATIONS); p=tmp_path/"n.md"; write_note(p, stale_after=(datetime.now(timezone.utc).date()-timedelta(days=1)).isoformat())
    index_note(db,p)
    assert search_notes(db,"alpha") == []


def test_draft_note_is_not_searchable(tmp_path):
    db=migrate(tmp_path/"db", PROJECT_MIGRATIONS); p=tmp_path/"n.md"; write_note(p,status="draft")
    index_note(db,p)
    assert search_notes(db,"alpha") == []
