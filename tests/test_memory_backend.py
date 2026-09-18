import pytest

from above_all.db import PROJECT_MIGRATIONS, migrate
from above_all.memory_backend import get_backend
from above_all.notes import index_note, list_candidates, parse_note


def make_scope(tmp_path):
    scope = tmp_path / ".above-all"
    (scope / "notes").mkdir(parents=True)
    (scope / "candidates").mkdir()
    return scope, migrate(scope / "assistant.db", PROJECT_MIGRATIONS)


def add_active_note(db, scope, title="Old fact", body="alpha beta"):
    from above_all.notes import create_note, set_note_status

    path = create_note(scope / "notes", title, body, "fact", ["file:test"])
    set_note_status(path, "active")
    index_note(db, path)
    return path.stem


def test_default_backend_is_sqlite_fts():
    assert get_backend().name == "sqlite_fts"
    assert get_backend("sqlite_fts").name == "sqlite_fts"


def test_unknown_backend_fails_loudly():
    with pytest.raises(ValueError, match="unknown memory backend"):
        get_backend("mem0_oss")


def test_candidate_is_not_active_memory(tmp_path):
    scope, db = make_scope(tmp_path)
    backend = get_backend()
    path = backend.create_candidate(
        scope, title="Session finding", body="alpha beta", note_type="summary",
        sources=["session:s1"], extra={"observer": "above-all"},
    )
    note = parse_note(path.read_text())
    assert note.metadata["status"] == "candidate"
    assert note.metadata["observer"] == "above-all"
    assert path.parent.name == "candidates"
    assert backend.search(db, "alpha") == []
    assert [c["id"] for c in list_candidates(scope / "candidates")] == [path.stem]


def test_approve_promotes_candidate_to_searchable(tmp_path):
    scope, db = make_scope(tmp_path)
    backend = get_backend()
    path = backend.create_candidate(
        scope, title="Finding", body="alpha beta", note_type="fact", sources=["session:s1"]
    )
    new_path = backend.approve(db, scope, path.stem)
    assert new_path.parent.name == "notes"
    assert not path.exists()
    assert backend.search(db, "alpha")[0]["title"] == "Finding"
    assert list_candidates(scope / "candidates") == []


def test_discard_marks_rejected_and_keeps_file(tmp_path):
    scope, _db = make_scope(tmp_path)
    backend = get_backend()
    path = backend.create_candidate(
        scope, title="Bad", body="gamma", note_type="fact", sources=["session:s1"]
    )
    backend.discard(scope, path.stem)
    assert parse_note(path.read_text()).metadata["status"] == "rejected"
    assert list_candidates(scope / "candidates") == []


def test_replace_supersedes_old_note_and_keeps_evidence(tmp_path):
    scope, db = make_scope(tmp_path)
    backend = get_backend()
    old_id = add_active_note(db, scope)
    path = backend.create_candidate(
        scope, title="New fact", body="alpha gamma", note_type="fact", sources=["session:s2"]
    )
    backend.approve(db, scope, path.stem, replaces=old_id)
    old = parse_note((scope / "notes" / f"{old_id}.md").read_text())
    assert old.metadata["status"] == "superseded"  # prior note and sources preserved
    assert old.metadata["sources"] == ["file:test"]
    new = parse_note((scope / "notes" / f"{path.stem}.md").read_text())
    assert new.metadata["replaces"] == f"note:{old_id}"
    results = backend.search(db, "alpha")
    assert [r["title"] for r in results] == ["New fact"]


def test_approve_requires_a_candidate(tmp_path):
    scope, db = make_scope(tmp_path)
    with pytest.raises(ValueError, match="no candidate"):
        get_backend().approve(db, scope, "missing")



def test_direct_approve_revalidates_against_current_active_memory(tmp_path):
    scope, db = make_scope(tmp_path)
    backend = get_backend()
    candidate = backend.create_candidate(
        scope, title="Candidate", body="same claim", note_type="fact", sources=["session:s1"]
    )
    add_active_note(db, scope, body="same claim")

    with pytest.raises(ValueError, match="duplicates active"):
        backend.approve(db, scope, candidate.stem)

    assert candidate.exists()
    assert db.execute("SELECT COUNT(*) FROM notes WHERE status='active'").fetchone()[0] == 1


@pytest.mark.parametrize("boundary", ["journal", "old_file", "new_file", "db", "commit"])
def test_direct_approve_rolls_back_every_file_and_db_boundary(tmp_path, boundary):
    from above_all.write_gate import promote_candidate

    scope, db = make_scope(tmp_path)
    old_id = add_active_note(db, scope, body="old claim")
    candidate = get_backend().create_candidate(
        scope, title="New", body="new claim", note_type="fact", sources=["session:s2"]
    )
    before_candidate = candidate.read_bytes()
    before_old = (scope / "notes" / f"{old_id}.md").read_bytes()

    def fail(at):
        if at == boundary:
            raise RuntimeError(f"fault at {at}")

    with pytest.raises(RuntimeError, match="fault"):
        promote_candidate(db, scope, candidate.stem, replaces=old_id, fault=fail)

    assert candidate.read_bytes() == before_candidate
    assert (scope / "notes" / f"{old_id}.md").read_bytes() == before_old
    assert not (scope / "notes" / f"{candidate.stem}.md").exists()
    assert not list((scope / "transactions").glob("*.json"))
    assert [row["title"] for row in get_backend().search(db, "old")] == ["Old fact"]


def test_recovery_rolls_back_leftover_promotion_journal(tmp_path):
    import json

    from above_all.write_gate import recover_promotions

    scope, db = make_scope(tmp_path)
    candidate = get_backend().create_candidate(
        scope, title="Candidate", body="recover me", note_type="fact", sources=["session:s1"]
    )
    target = scope / "notes" / f"{candidate.stem}.md"
    target.write_text(candidate.read_text().replace("status: candidate", "status: active"))
    index_note(db, target)
    journal = scope / "transactions" / "promotion-interrupted.json"
    journal.parent.mkdir()
    journal.write_text(json.dumps({
        "candidate_id": candidate.stem,
        "files": [
            {"path": str(candidate), "before": candidate.read_bytes().hex()},
            {"path": str(target), "before": None},
        ],
    }))

    assert recover_promotions(db, scope) == [candidate.stem]
    assert candidate.exists() and not target.exists() and not journal.exists()
    assert get_backend().search(db, "recover") == []
