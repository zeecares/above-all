import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from above_all.db import PROJECT_MIGRATIONS, migrate
from above_all.knowledge import why
from above_all.memory_backend import get_backend


def make_scope(tmp_path):
    scope = tmp_path / ".above-all"
    (scope / "notes").mkdir(parents=True)
    (scope / "candidates").mkdir()
    return scope, migrate(scope / "assistant.db", PROJECT_MIGRATIONS)


def candidate(backend, scope, title, body, **extra):
    return backend.create_candidate(
        scope, title=title, body=body, note_type="fact", sources=["message:m1#L4"], extra=extra
    )


def test_map_is_written_only_by_review_gate_and_why_traces_sources(tmp_path):
    scope, db = make_scope(tmp_path)
    backend = get_backend()
    path = candidate(
        backend, scope, "Ownership", "Alice owns service-red", claim_kind="premise",
        entities=[{"id": "alice", "name": "Alice", "type": "person"},
                  {"id": "service-red", "name": "Service Red", "type": "service"}],
        relations=[{"subject": "alice", "predicate": "owns", "object": "service-red"}],
    )
    assert db.execute("SELECT count(*) FROM knowledge_claims").fetchone()[0] == 0
    backend.approve(db, scope, path.stem)
    result = why(db, "Alice")
    assert result["found"] is True
    assert result["claim"]["claim_kind"] == "premise"
    assert result["claim"]["scope"] == "project"
    assert result["sources"] == ["message:m1#L4"]
    assert result["relations"] == [{
        "subject_entity_id": "alice", "predicate": "owns",
        "object_entity_id": "service-red", "confidence": 1.0,
    }]


def test_supersession_and_contradiction_edges_are_visible(tmp_path):
    scope, db = make_scope(tmp_path)
    backend = get_backend()
    old = candidate(backend, scope, "Old", "budget is 10")
    backend.approve(db, scope, old.stem)
    new = candidate(backend, scope, "New", "budget is 20", claim_kind="inference", confidence=0.8)
    backend.approve(db, scope, new.stem, replaces=old.stem, contradicts=[old.stem])
    result = why(db, "budget")
    assert result["claim"]["id"] == f"note:{new.stem}"
    assert {edge["edge_type"] for edge in result["edges"]} == {"supersedes", "contradicts"}
    old_row = db.execute("SELECT status FROM knowledge_claims WHERE id=?", (f"note:{old.stem}",)).fetchone()
    assert old_row[0] == "superseded"


def test_stale_claim_is_excluded_from_why(tmp_path):
    scope, db = make_scope(tmp_path)
    backend = get_backend()
    stale = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    path = backend.create_candidate(
        scope, title="Old region", body="region was west", note_type="fact",
        sources=["file:old#L1"], stale_after=stale,
    )
    backend.approve(db, scope, path.stem)
    assert why(db, f"note:{path.stem}")["found"] is False
    assert db.execute("SELECT count(*) FROM knowledge_claims_fts").fetchone()[0] == 0


def test_cli_memory_why_and_explain_alias(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    env = dict(os.environ, ABOVE_ALL_HOME=str(tmp_path / "home"))
    cli = [sys.executable, "-m", "above_all.cli"]
    subprocess.run([*cli, "init"], cwd=repo, env=env, check=True, capture_output=True)
    scope = repo / ".above-all"
    db = migrate(scope / "assistant.db", PROJECT_MIGRATIONS)
    path = candidate(get_backend(), scope, "Citrus", "oranges are orange")
    get_backend().approve(db, scope, path.stem)
    db.close()
    for verb in ("why", "explain"):
        out = subprocess.run([*cli, "memory", verb, "oranges"], cwd=repo, env=env,
                             check=True, capture_output=True, text=True)
        assert json.loads(out.stdout)["sources"] == ["message:m1#L4"]


def test_contradiction_edge_requires_an_active_target(tmp_path):
    import pytest

    scope, db = make_scope(tmp_path)
    backend = get_backend()
    path = candidate(backend, scope, "Claim", "a claim")
    with pytest.raises(ValueError, match="active notes"):
        backend.approve(db, scope, path.stem, contradicts=["missing"] )
    assert path.exists()
    assert db.execute("SELECT count(*) FROM knowledge_claims").fetchone()[0] == 0
