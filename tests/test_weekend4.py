import json
from datetime import date, datetime, timezone

import pytest

from above_all.consolidation import apply_changeset, daily_expiry_sweep, propose_weekly
from above_all.db import GLOBAL_MIGRATIONS, PROJECT_MIGRATIONS, migrate
from above_all.notes import create_candidate, index_note, parse_note
from above_all.proactivity import create_watch, due_watches, fire_watch, value_gate


def note(path, body, status="active", stale_after=None):
    path.write_text(f"---\ntype: fact\nsources: [file:test]\ngenerated: 2026-09-17T00:00:00Z\nverified: source\nstatus: {status}\nstale_after: {stale_after or 'null'}\n---\n# Fact\n\n{body}\n")
    return path


def scope(tmp_path):
    root = tmp_path / ".above-all"; (root / "notes").mkdir(parents=True); (root / "candidates").mkdir()
    return root, migrate(root / "assistant.db", PROJECT_MIGRATIONS)


def test_daily_expiry_marks_needs_review_and_never_touches_candidate(tmp_path):
    root, db = scope(tmp_path)
    old = note(root / "notes" / "old.md", "old fact", stale_after="2026-01-01"); index_note(db, old)
    candidate = create_candidate(root / "candidates", "Maybe", "candidate", "fact", ["trace:t1"])
    result = daily_expiry_sweep(db, root, date(2026, 9, 18))
    assert result == {"expired": ["old"], "candidates_touched": 0}
    assert parse_note(old.read_text()).metadata["status"] == "needs-review"
    assert parse_note(candidate.read_text()).metadata["status"] == "candidate"
    assert db.execute("SELECT COUNT(*) FROM notes_fts").fetchone()[0] == 0


def test_weekly_is_bounded_and_apply_requires_explicit_approve(tmp_path):
    root, db = scope(tmp_path)
    active = note(root / "notes" / "a.md", "alpha beta"); index_note(db, active)
    duplicate = create_candidate(root / "candidates", "Dup", "# Fact\n\nalpha beta", "fact", ["trace:t1"])
    reviewed = create_candidate(root / "candidates", "New", "gamma delta", "fact", ["trace:t2"])
    reviewed.write_text(reviewed.read_text().replace("status: candidate", "status: reviewed"))
    analysis = tmp_path / "analysis"; analysis.mkdir(); (analysis / "system-candidates.json").write_text("[]\n")
    changeset = propose_weekly(db, root, tmp_path / "changes", analysis, max_candidates=10)
    payload = json.loads(changeset.read_text())
    assert payload["requires_explicit_approve"] is True and payload["trace_analysis_inputs"]
    assert {x["action"] for x in payload["changes"]} == {"reject_duplicate", "promote"}
    with pytest.raises(ValueError, match="explicit approve"): apply_changeset(db, root, changeset)
    assert duplicate.exists() and reviewed.exists()
    result = apply_changeset(db, root, changeset, approve=True)
    assert result["status"] == "applied"
    assert parse_note(duplicate.read_text()).metadata["status"] == "rejected"
    promoted = root / "notes" / f"{reviewed.stem}.md"
    assert parse_note(promoted.read_text()).metadata["status"] == "active"


def test_contradiction_is_flagged_and_preserves_active_evidence(tmp_path):
    root, db = scope(tmp_path)
    active = note(root / "notes" / "a.md", "deploy production on friday"); index_note(db, active)
    candidate = create_candidate(root / "candidates", "Changed", "do not deploy production on friday", "procedure", ["trace:t3"])
    path = propose_weekly(db, root, tmp_path / "changes")
    change = json.loads(path.read_text())["changes"][0]
    assert change == {"action": "flag_contradiction", "active_ids": ["a"], "candidate_id": candidate.stem}
    apply_changeset(db, root, path, approve=True)
    assert parse_note(active.read_text()).metadata["status"] == "active"
    meta = parse_note(candidate.read_text()).metadata
    assert meta["status"] == "needs-review" and meta["contradicts"] == ["note:a"]


def test_clock_and_cadence_watches_persist_and_fire_internal_events(tmp_path):
    db = migrate(tmp_path / "global.db", GLOBAL_MIGRATIONS)
    clock = create_watch(db, "clock", {"at": "2026-09-18T09:00:00Z"}, "2026-09-18T09:00:00+00:00")
    cadence = create_watch(db, "cadence", {"minutes": 60}, "2026-09-18T09:00:00+00:00")
    assert [x["id"] for x in due_watches(db, "2026-09-18T10:00:00+00:00")] == [cadence, clock]
    now = datetime(2026, 9, 18, 10, tzinfo=timezone.utc)
    fire_watch(db, clock, {"risk": "deadline"}, value="avoids a missed deadline", now=now)
    fire_watch(db, cadence, {"changed": False}, now=now)
    gated = value_gate(db)
    assert len(gated["surfaced"]) == 1 and gated["surfaced"][0]["value"] == "avoids a missed deadline"
    assert gated["internal_count"] == 1
    assert db.execute("SELECT next_fire_at FROM watches WHERE id=?", (clock,)).fetchone()[0] is None
    assert db.execute("SELECT next_fire_at FROM watches WHERE id=?", (cadence,)).fetchone()[0].startswith("2026-09-18T11:00:00")


def test_invalid_cadence_fails_loudly(tmp_path):
    db = migrate(tmp_path / "global.db", GLOBAL_MIGRATIONS)
    with pytest.raises(ValueError, match="positive integer"): create_watch(db, "cadence", {"minutes": 0})
