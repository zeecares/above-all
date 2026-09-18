import json
from datetime import date, datetime, timezone

import pytest

from above_all.consolidation import (
    apply_changeset,
    daily_expiry_sweep,
    pollution_metrics,
    propose_weekly,
)
from above_all.db import GLOBAL_MIGRATIONS, PROJECT_MIGRATIONS, migrate
from above_all.notes import approve_candidate, create_candidate, index_note, parse_note
from above_all.proactivity import create_watch, due_watches, fire_watch, value_gate


def note(path, body, status="active", stale_after=None, sources="[file:test]"):
    path.write_text(f"---\ntype: fact\nsources: {sources}\ngenerated: 2026-09-17T00:00:00Z\nverified: source\nstatus: {status}\nstale_after: {stale_after or 'null'}\n---\n# Fact\n\n{body}\n")
    return path


def scope(tmp_path):
    root = tmp_path / ".above-all"; (root / "notes").mkdir(parents=True); (root / "candidates").mkdir()
    return root, migrate(root / "assistant.db", PROJECT_MIGRATIONS)


def reviewed(path):
    path.write_text(path.read_text().replace("status: candidate", "status: reviewed")); return path


def classifier(category="risk"):
    return lambda value, payload: {"category": category, "reason": value}


def test_daily_expiry_marks_needs_review_and_never_touches_candidate(tmp_path):
    root, db = scope(tmp_path)
    old = note(root / "notes" / "old.md", "old fact", stale_after="2026-01-01"); index_note(db, old)
    boundary = note(root / "notes" / "boundary.md", "today fact", stale_after="2026-09-18"); index_note(db, boundary)
    future = note(root / "notes" / "future.md", "future fact", stale_after="2027-01-01"); index_note(db, future)
    candidate = create_candidate(root / "candidates", "Maybe", "candidate", "fact", ["trace:t1"], stale_after="2026-01-01")
    result = daily_expiry_sweep(db, root, date(2026, 9, 18))
    assert result == {"expired": ["old"], "candidates_touched": 0}
    assert parse_note(old.read_text()).metadata["status"] == "needs-review"
    assert parse_note(boundary.read_text()).metadata["status"] == "active"
    assert parse_note(future.read_text()).metadata["status"] == "active"
    assert parse_note(candidate.read_text()).metadata["status"] == "candidate"
    assert db.execute("SELECT COUNT(*) FROM notes_fts WHERE note_id='old'").fetchone()[0] == 0


def test_daily_expiry_missing_file_fails_before_any_mutation(tmp_path):
    root, db = scope(tmp_path)
    a = note(root / "notes" / "a.md", "a", stale_after="2026-01-01"); index_note(db, a)
    b = note(root / "notes" / "b.md", "b", stale_after="2026-01-01"); index_note(db, b); b.unlink()
    with pytest.raises(FileNotFoundError): daily_expiry_sweep(db, root, date(2026, 9, 18))
    assert parse_note(a.read_text()).metadata["status"] == "active"


def test_weekly_is_bounded_and_apply_requires_explicit_approve(tmp_path):
    root, db = scope(tmp_path)
    active = note(root / "notes" / "a.md", "alpha beta"); index_note(db, active)
    duplicate = create_candidate(root / "candidates", "Dup", "alpha beta", "fact", ["trace:t1"])
    new = reviewed(create_candidate(root / "candidates", "New", "gamma delta", "fact", ["trace:t2"]))
    changeset = propose_weekly(db, root, tmp_path / "changes", max_candidates=10)
    payload = json.loads(changeset.read_text())
    assert payload["requires_explicit_approve"] is True
    assert {x["action"] for x in payload["changes"]} == {"reject_duplicate", "promote"}
    assert all("candidate_sha256" in x for x in payload["changes"])
    with pytest.raises(ValueError, match="explicit approve"): apply_changeset(db, root, changeset)
    result = apply_changeset(db, root, changeset, approve=True)
    assert result["status"] == "applied"
    assert parse_note(duplicate.read_text()).metadata["status"] == "rejected"
    assert parse_note((root / "notes" / f"{new.stem}.md").read_text()).metadata["status"] == "active"
    assert apply_changeset(db, root, changeset, approve=True)["idempotent"] is True


def test_apply_revalidates_and_returns_drifted_candidate_to_review(tmp_path):
    root, db = scope(tmp_path)
    candidate = reviewed(create_candidate(root / "candidates", "New", "same claim", "fact", ["trace:t1"]))
    changeset = propose_weekly(db, root, tmp_path / "changes")
    other = create_candidate(root / "candidates", "Other", "same claim", "fact", ["trace:t2"]); approve_candidate(db, root, other.stem)
    result = apply_changeset(db, root, changeset, approve=True)
    assert result["status"] == "drifted" and result["drifted_candidates"] == [candidate.stem]
    assert parse_note(candidate.read_text()).metadata["status"] == "needs-review"
    assert db.execute("SELECT COUNT(*) FROM notes WHERE status='active'").fetchone()[0] == 1


def test_changeset_rejects_unknown_or_tampered_action_before_writes(tmp_path):
    root, db = scope(tmp_path)
    candidate = reviewed(create_candidate(root / "candidates", "New", "gamma", "fact", ["trace:t1"]))
    changeset = propose_weekly(db, root, tmp_path / "changes")
    payload = json.loads(changeset.read_text()); payload["changes"].append({"action": "erase_everything"}); changeset.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="unknown"): apply_changeset(db, root, changeset, approve=True)
    assert parse_note(candidate.read_text()).metadata["status"] == "reviewed"


def test_replace_supersedes_old_and_preserves_evidence(tmp_path):
    root, db = scope(tmp_path)
    active = note(root / "notes" / "a.md", "deploy production on friday", sources="[trace:old]"); index_note(db, active)
    candidate = reviewed(create_candidate(root / "candidates", "Changed", "do not deploy production on friday", "procedure", ["trace:new"]))
    path = propose_weekly(db, root, tmp_path / "changes")
    assert json.loads(path.read_text())["changes"][0]["action"] == "replace"
    apply_changeset(db, root, path, approve=True)
    old = parse_note(active.read_text()); new = parse_note((root / "notes" / f"{candidate.stem}.md").read_text())
    assert old.metadata["status"] == "superseded" and old.metadata["sources"] == ["trace:old"]
    assert new.metadata["status"] == "active" and new.metadata["replaces"] == "note:a"


def test_candidate_contradiction_stays_for_review_and_preserves_active(tmp_path):
    root, db = scope(tmp_path)
    active = note(root / "notes" / "a.md", "deploy production on friday"); index_note(db, active)
    candidate = create_candidate(root / "candidates", "Changed", "do not deploy production on friday", "procedure", ["trace:t3"])
    path = propose_weekly(db, root, tmp_path / "changes"); apply_changeset(db, root, path, approve=True)
    assert parse_note(active.read_text()).metadata["status"] == "active"
    meta = parse_note(candidate.read_text()).metadata
    assert meta["status"] == "needs-review" and meta["contradicts"] == ["note:a"]


def test_trace_queues_are_bounded_ingested_proposals_and_malformed_fails(tmp_path):
    root, db = scope(tmp_path); analysis = tmp_path / "analysis"; analysis.mkdir()
    item = {"kind": "repeated_tool_failure", "evidence_sessions": ["s1", "s2"], "estimated_value": "save retry"}
    (analysis / "system-candidates.json").write_text(json.dumps([item, {"kind": "second"}]))
    path = propose_weekly(db, root, tmp_path / "changes", analysis, max_candidates=1)
    payload = json.loads(path.read_text()); traces = [x for x in payload["changes"] if x["action"] == "trace_candidate"]
    assert len(traces) == 1 and traces[0]["proposal"] == item and payload["trace_analysis_inputs"][0]["items"] == 2
    assert not list((root / "candidates").iterdir())
    (analysis / "bad-candidates.json").write_text("{")
    with pytest.raises(ValueError, match="malformed trace queue"): propose_weekly(db, root, tmp_path / "changes2", analysis)


def test_watch_identity_is_unique_idempotent_and_persists(tmp_path):
    path = tmp_path / "global.db"; db = migrate(path, GLOBAL_MIGRATIONS)
    one = create_watch(db, "clock", {"at": "2026-09-18T09:00:00Z"}, "2026-09-18T09:00:00+00:00")
    two = create_watch(db, "clock", {"at": "2026-09-18T09:00:00Z"}, "2026-09-18T10:00:00+00:00")
    assert one == two and db.execute("SELECT COUNT(*) FROM watches").fetchone()[0] == 1
    db.close(); db = migrate(path, GLOBAL_MIGRATIONS)
    assert db.execute("SELECT next_fire_at FROM watches WHERE id=?", (one,)).fetchone()[0] == "2026-09-18T10:00:00+00:00"


def test_value_gate_fails_closed_honors_policy_and_accepts_only_categories(tmp_path):
    db = migrate(tmp_path / "global.db", GLOBAL_MIGRATIONS)
    good = create_watch(db, "clock", {"at": "good"}, "2026-09-18T09:00:00+00:00")
    bad = create_watch(db, "clock", {"at": "bad"}, "2026-09-18T09:00:00+00:00")
    internal = create_watch(db, "clock", {"at": "internal"}, "2026-09-18T09:00:00+00:00", policy="internal-only")
    now = datetime(2026, 9, 18, 10, tzinfo=timezone.utc)
    fire_watch(db, good, {"risk": "deadline"}, value="avoids missed deadline", now=now)
    fire_watch(db, bad, {"noise": True}, value="hello", now=now)
    fire_watch(db, internal, {"risk": True}, value="real risk", now=now)
    decisions = iter([{"category": "risk", "reason": "avoids missed deadline"}, {"category": "bogus", "reason": "hello"}])
    gated = value_gate(db, lambda value, payload: next(decisions))
    assert len(gated["surfaced"]) == 1 and gated["surfaced"][0]["value_category"] == "risk"
    assert gated["internal_count"] == 2
    quiet = create_watch(db, "clock", {"at": "quiet"}, "2026-09-18T09:00:00+00:00")
    fire_watch(db, quiet, {}, value="risk", now=now)
    assert value_gate(db)["internal_count"] == 1  # placeholder is fail closed


def test_cadence_reschedules_and_invalid_fails(tmp_path):
    db = migrate(tmp_path / "global.db", GLOBAL_MIGRATIONS)
    cadence = create_watch(db, "cadence", {"minutes": 60}, "2026-09-18T09:00:00+00:00")
    assert due_watches(db, "2026-09-18T10:00:00+00:00")[0]["id"] == cadence
    fire_watch(db, cadence, {}, now=datetime(2026, 9, 18, 10, tzinfo=timezone.utc))
    assert db.execute("SELECT next_fire_at FROM watches WHERE id=?", (cadence,)).fetchone()[0].startswith("2026-09-18T11:00:00")
    with pytest.raises(ValueError, match="positive integer"): create_watch(db, "cadence", {"minutes": 0})


def test_pollution_metrics_expose_all_tripwire_groups(tmp_path):
    root, db = scope(tmp_path)
    db.execute("INSERT INTO events(ts,kind,payload_json) VALUES ('2026-09-18','note_retrieved','{}'),('2026-09-18','retrieval_corrected','{}'),('2026-09-18','empty_extraction_batch','{}')")
    metrics = pollution_metrics(db, root)
    assert {"admission", "recent_replacements_or_contradictions", "retrieval_recall", "extraction", "prompt_memory_tokens_per_completed_outcome"} <= metrics.keys()
    assert metrics["retrieval_recall"]["ignored_or_corrected"] == 1
    assert metrics["extraction"] == {"failures_or_empty_batches": 1, "visible": True}



def test_operational_runner_sweeps_fires_and_is_restart_idempotent(tmp_path):
    from above_all.operations import run_maintenance

    root, db = scope(tmp_path)
    old = note(root / "notes" / "old.md", "old", stale_after="2026-01-01")
    index_note(db, old)
    watch = create_watch(db, "clock", {"at": "now"}, "2026-01-01T00:00:00+00:00")
    first = run_maintenance(db, root)
    assert first["expiry"]["expired"] == ["old"]
    assert first["watches"]["fired"][0]["watch_id"] == watch
    db.close()
    db = migrate(root / "assistant.db", PROJECT_MIGRATIONS)
    second = run_maintenance(db, root)
    assert second["expiry"]["expired"] == [] and second["watches"]["fired"] == []


def test_cli_reaches_sweep_weekly_watch_and_explicit_apply(tmp_path, monkeypatch, capsys):
    import subprocess

    from above_all.cli import main

    repo = tmp_path / "repo"; repo.mkdir(); subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    monkeypatch.chdir(repo); monkeypatch.setenv("ABOVE_ALL_HOME", str(tmp_path / "home"))
    main(["init"])
    capsys.readouterr()
    assert main(["watch", "create", "clock", '{"at":"later"}', "--next-fire", "2026-01-01T00:00:00+00:00"]) == 0
    assert "id" in json.loads(capsys.readouterr().out)
    assert main(["consolidate", "sweep"]) == 0
    assert "expired" in json.loads(capsys.readouterr().out)
    assert main(["consolidate", "weekly", "--max-candidates", "1"]) == 0
    changeset = json.loads(capsys.readouterr().out)["path"]
    with pytest.raises(ValueError, match="explicit approve"):
        main(["consolidate", "apply", changeset])
    assert main(["consolidate", "apply", changeset, "--approve"]) == 0
