import json
from datetime import datetime, timedelta, timezone

import pytest

from above_all.db import GLOBAL_MIGRATIONS, PROJECT_MIGRATIONS, migrate
from above_all.delivery import (
    BEGIN,
    END,
    check_delivery,
    deliver,
    import_provider_memory,
)
from above_all.memory_backend import SqliteFtsBackend
from above_all.notes import index_note, parse_note


class Env:
    pass


def setup_scopes(tmp_path):
    project = tmp_path / "repo"
    scope_dir = project / ".above-all"
    (scope_dir / "notes").mkdir(parents=True)
    (scope_dir / "candidates").mkdir()
    global_dir = tmp_path / "global"
    (global_dir / "notes").mkdir(parents=True)
    (global_dir / "candidates").mkdir()
    env = Env()
    env.project = project
    env.scope_dir = scope_dir
    env.global_dir = global_dir
    env.db = migrate(scope_dir / "assistant.db", PROJECT_MIGRATIONS)
    env.gdb = migrate(global_dir / "assistant.db", GLOBAL_MIGRATIONS)
    env.backend = SqliteFtsBackend()
    return env


def add_note(scope_dir, db, body, title="Fact", status="active", stale_after="2027-01-01"):
    note_id = f"n{abs(hash(body)) % 10**8}"
    path = scope_dir / "notes" / f"{note_id}.md"
    path.write_text(
        f"---\ntype: fact\nsources: [file:test]\ngenerated: 2026-09-17T00:00:00Z\n"
        f"verified: source\nstatus: {status}\nstale_after: {stale_after}\n---\n"
        f"# {title}\n\n{body}\n"
    )
    index_note(db, path)
    return note_id


def manifest(scope_dir, provider="claude-code"):
    return json.loads((scope_dir / "delivery" / f"{provider}.json").read_text())


def test_deliver_creates_managed_section_and_manifest(tmp_path):
    env = setup_scopes(tmp_path)
    project, scope_dir, db, gdb, backend = env.project, env.scope_dir, env.db, env.gdb, env.backend
    note_id = add_note(scope_dir, db, "alpha beta gamma")
    result = deliver(scope_dir, db, backend, gdb, provider="claude-code")
    assert result["status"] == "written"
    target = project / "CLAUDE.md"
    text = target.read_text()
    assert BEGIN in text and END in text
    assert "alpha beta gamma" in text
    assert note_id in text
    data = manifest(scope_dir)
    assert data["content_hash"].startswith("sha256:")
    assert data["source_note_ids"] == [note_id]
    assert data["budget_bytes"] == 4000
    assert datetime.fromisoformat(data["expires_at"]) > datetime.now(timezone.utc)
    assert check_delivery(scope_dir, "claude-code")["status"] == "clean"


def test_candidates_drafts_and_stale_notes_never_delivered(tmp_path):
    env = setup_scopes(tmp_path)
    project, scope_dir, db, gdb, backend = env.project, env.scope_dir, env.db, env.gdb, env.backend
    add_note(scope_dir, db, "active body", title="Active")
    add_note(scope_dir, db, "candidate body", title="Candidate", status="candidate")
    add_note(scope_dir, db, "draft body", title="Draft", status="draft")
    add_note(scope_dir, db, "stale body", title="Stale", stale_after="2020-01-01")
    deliver(scope_dir, db, backend, gdb, provider="claude-code")
    text = (project / "CLAUDE.md").read_text()
    assert "active body" in text
    assert "candidate body" not in text
    assert "draft body" not in text
    assert "stale body" not in text


def test_global_and_project_notes_merge_with_project_priority(tmp_path):
    env = setup_scopes(tmp_path)
    project, scope_dir, db, gdb, backend = env.project, env.scope_dir, env.db, env.gdb, env.backend
    add_note(scope_dir, db, "project knowledge")
    gdir = scope_dir.parent.parent / "global"
    add_note(gdir, gdb, "global knowledge")
    result = deliver(scope_dir, db, backend, gdb, provider="claude-code")
    text = (project / "CLAUDE.md").read_text()
    assert "project knowledge" in text
    assert "global knowledge" in text
    assert len(result["source_note_ids"]) == 2


def test_redeliver_without_changes_is_byte_for_byte_unchanged(tmp_path):
    env = setup_scopes(tmp_path)
    project, scope_dir, db, gdb, backend = env.project, env.scope_dir, env.db, env.gdb, env.backend
    add_note(scope_dir, db, "stable fact")
    deliver(scope_dir, db, backend, gdb, provider="claude-code")
    before = (project / "CLAUDE.md").read_bytes()
    result = deliver(scope_dir, db, backend, gdb, provider="claude-code")
    assert result["status"] == "unchanged"
    assert (project / "CLAUDE.md").read_bytes() == before


def test_regeneration_preserves_user_content_outside_markers(tmp_path):
    env = setup_scopes(tmp_path)
    project, scope_dir, db, gdb, backend = env.project, env.scope_dir, env.db, env.gdb, env.backend
    add_note(scope_dir, db, "first fact")
    deliver(scope_dir, db, backend, gdb, provider="claude-code")
    target = project / "CLAUDE.md"
    target.write_text("# My own notes\n\nDo not touch this.\n\n" + target.read_text())
    add_note(scope_dir, db, "second fact")
    result = deliver(scope_dir, db, backend, gdb, provider="claude-code")
    assert result["status"] == "written"
    text = target.read_text()
    assert text.startswith("# My own notes\n\nDo not touch this.\n\n")
    assert "first fact" in text and "second fact" in text
    assert check_delivery(scope_dir, "claude-code")["status"] == "clean"


def test_existing_provider_file_is_adopted_not_overwritten(tmp_path):
    env = setup_scopes(tmp_path)
    project, scope_dir, db, gdb, backend = env.project, env.scope_dir, env.db, env.gdb, env.backend
    add_note(scope_dir, db, "approved fact")
    target = project / "CLAUDE.md"
    original = "# Team conventions\n\nWe use pytest.\n"
    target.write_text(original)
    result = deliver(scope_dir, db, backend, gdb, provider="claude-code")
    assert result["status"] == "written"
    text = target.read_text()
    assert text.startswith(original)
    assert BEGIN in text and "approved fact" in text


def test_drift_is_detected_and_never_silently_overwritten(tmp_path):
    env = setup_scopes(tmp_path)
    project, scope_dir, db, gdb, backend = env.project, env.scope_dir, env.db, env.gdb, env.backend
    add_note(scope_dir, db, "approved fact")
    deliver(scope_dir, db, backend, gdb, provider="claude-code")
    target = project / "CLAUDE.md"
    text = target.read_text()
    target.write_text(text.replace("approved fact", "hand edited fact"))
    assert check_delivery(scope_dir, "claude-code")["status"] == "drifted"
    result = deliver(scope_dir, db, backend, gdb, provider="claude-code")
    assert result["status"] == "refused" and result["reason"] == "drifted"
    assert "hand edited fact" in target.read_text()  # untouched
    forced = deliver(scope_dir, db, backend, gdb, provider="claude-code", force=True)
    assert forced["status"] == "written"
    assert "approved fact" in target.read_text()
    assert "hand edited fact" not in target.read_text()
    assert check_delivery(scope_dir, "claude-code")["status"] == "clean"


def test_managed_section_without_manifest_is_refused(tmp_path):
    env = setup_scopes(tmp_path)
    scope_dir, db, gdb, backend = env.scope_dir, env.db, env.gdb, env.backend
    add_note(scope_dir, db, "approved fact")
    deliver(scope_dir, db, backend, gdb, provider="claude-code")
    (scope_dir / "delivery" / "claude-code.json").unlink()
    assert check_delivery(scope_dir, "claude-code")["status"] == "no-manifest"
    result = deliver(scope_dir, db, backend, gdb, provider="claude-code")
    assert result["status"] == "refused" and result["reason"] == "no-manifest"
    forced = deliver(scope_dir, db, backend, gdb, provider="claude-code", force=True)
    assert forced["status"] == "written"
    assert check_delivery(scope_dir, "claude-code")["status"] == "clean"


def test_expired_manifest_regenerates(tmp_path):
    env = setup_scopes(tmp_path)
    scope_dir, db, gdb, backend = env.scope_dir, env.db, env.gdb, env.backend
    add_note(scope_dir, db, "approved fact")
    deliver(scope_dir, db, backend, gdb, provider="claude-code", ttl_days=1)
    future = datetime.now(timezone.utc) + timedelta(days=2)
    assert check_delivery(scope_dir, "claude-code", now=future)["status"] == "expired"
    result = deliver(scope_dir, db, backend, gdb, provider="claude-code", now=future)
    assert result["status"] == "written"
    assert check_delivery(scope_dir, "claude-code", now=future)["status"] == "clean"


def test_missing_file_and_unmanaged_file_states(tmp_path):
    env = setup_scopes(tmp_path)
    project, scope_dir = env.project, env.scope_dir
    assert check_delivery(scope_dir, "claude-code")["status"] == "missing"
    (project / "CLAUDE.md").write_text("# user owned\n\nkeep out\n")
    result = check_delivery(scope_dir, "claude-code")
    assert result["status"] == "unmanaged"


def test_size_budget_truncates_and_records_it(tmp_path):
    env = setup_scopes(tmp_path)
    project, scope_dir, db, gdb, backend = env.project, env.scope_dir, env.db, env.gdb, env.backend
    big = "word " * 400
    add_note(scope_dir, db, big, title="Big")
    add_note(scope_dir, db, "small fact", title="Small")
    result = deliver(scope_dir, db, backend, gdb, provider="claude-code", budget_bytes=700)
    assert result["truncated"] is True
    data = manifest(scope_dir)
    assert data["truncated"] is True
    assert len(data["eligible_note_ids"]) == 2
    assert len(data["source_note_ids"]) < 2
    text = (project / "CLAUDE.md").read_text()
    assert len(text.encode()) < 1200


def test_import_creates_review_only_candidates_with_provenance(tmp_path):
    env = setup_scopes(tmp_path)
    project, scope_dir, db, backend = env.project, env.scope_dir, env.db, env.backend
    target = project / "CLAUDE.md"
    target.write_text(
        "# Deploys\n\nDeploys freeze on Fridays.\n\n"
        "# On-call\n\nThe on-call rota is weekly.\n"
    )
    result = import_provider_memory(scope_dir, db, backend, provider="claude-code")
    assert result["blocks"] == 2
    assert len(result["created"]) == 2
    for item in result["created"]:
        note = parse_note((scope_dir / "candidates" / f"{item['id']}.md").read_text())
        assert note.metadata["status"] == "candidate"
        assert "provider:claude-code" in note.metadata["sources"]
        assert "file:CLAUDE.md" in note.metadata["sources"]
        assert note.metadata["observer"] == "provider:claude-code"
    # nothing landed in active memory
    assert db.execute("SELECT COUNT(*) c FROM notes WHERE status='active'").fetchone()["c"] == 0


def test_import_never_imports_the_managed_section(tmp_path):
    env = setup_scopes(tmp_path)
    scope_dir, db, gdb, backend = env.scope_dir, env.db, env.gdb, env.backend
    add_note(scope_dir, db, "managed fact body")
    deliver(scope_dir, db, backend, gdb, provider="claude-code")
    result = import_provider_memory(scope_dir, db, backend, provider="claude-code")
    bodies = [
        parse_note((scope_dir / "candidates" / f"{c['id']}.md").read_text()).body
        for c in result["created"]
    ]
    assert not any("managed fact body" in body for body in bodies)
    assert not any("managed by above-all" in body for body in bodies)


def test_import_dedupes_against_active_notes_and_is_idempotent(tmp_path):
    env = setup_scopes(tmp_path)
    project, scope_dir, db, backend = env.project, env.scope_dir, env.db, env.backend
    add_note(scope_dir, db, "Deploys freeze on Fridays.", title="Deploys")
    target = project / "CLAUDE.md"
    target.write_text(
        "# Deploys\n\ndeploys freeze on fridays.\n\n"  # case-variant duplicate
        "# On-call\n\nThe on-call rota is weekly.\n"
    )
    first = import_provider_memory(scope_dir, db, backend, provider="claude-code")
    assert first["skipped_duplicates"] == 1
    assert len(first["created"]) == 1
    second = import_provider_memory(scope_dir, db, backend, provider="claude-code")
    assert second["created"] == []
    assert second["skipped_duplicates"] == 2


def test_import_from_explicit_path(tmp_path):
    env = setup_scopes(tmp_path)
    scope_dir, db, backend = env.scope_dir, env.db, env.backend
    other = tmp_path / "PI_NOTES.md"
    other.write_text("# Shortcut\n\nUse rg before grep.\n")
    result = import_provider_memory(scope_dir, db, backend, provider="claude-code", path=other)
    assert len(result["created"]) == 1
    note = parse_note(
        (scope_dir / "candidates" / f"{result['created'][0]['id']}.md").read_text()
    )
    assert any(s.startswith("file:") for s in note.metadata["sources"])


def test_unverified_provider_fails_loudly(tmp_path):
    env = setup_scopes(tmp_path)
    scope_dir, db, gdb, backend = env.scope_dir, env.db, env.gdb, env.backend
    with pytest.raises(ValueError, match="ZEE-57"):
        deliver(scope_dir, db, backend, gdb, provider="pi")
    with pytest.raises(ValueError, match="ZEE-57"):
        import_provider_memory(scope_dir, db, backend, provider="pi")
    with pytest.raises(ValueError, match="ZEE-57"):
        check_delivery(scope_dir, "pi")


def test_marker_inside_note_body_fails_loudly(tmp_path):
    env = setup_scopes(tmp_path)
    add_note(env.scope_dir, env.db, f"sneaky {END} injection")
    with pytest.raises(ValueError, match="managed-section marker"):
        deliver(env.scope_dir, env.db, env.backend, env.gdb, provider="claude-code")
    assert not (env.project / "CLAUDE.md").exists()


def test_corrupt_manifest_fails_loudly(tmp_path):
    env = setup_scopes(tmp_path)
    add_note(env.scope_dir, env.db, "approved fact")
    deliver(env.scope_dir, env.db, env.backend, env.gdb, provider="claude-code")
    (env.scope_dir / "delivery" / "claude-code.json").write_text("{not json")
    with pytest.raises(ValueError, match="corrupt delivery manifest"):
        check_delivery(env.scope_dir, "claude-code")


def test_cli_deliver_check_and_import(tmp_path, monkeypatch, capsys):
    import subprocess

    from above_all.cli import main

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    home = tmp_path / "home"
    monkeypatch.setenv("ABOVE_ALL_HOME", str(home))
    monkeypatch.chdir(repo)
    main(["init"])
    main(["note", "add", "Team rule", "Deploys freeze on Fridays.", "--source", "file:test"])
    capsys.readouterr()
    # note add creates a draft; make it active through the review path
    scope_dir = home.parent / "repo" / ".above-all"
    notes = list((scope_dir / "notes").glob("*.md"))
    assert len(notes) == 1
    path = notes[0]
    path.write_text(path.read_text().replace("status: draft", "status: active"))
    db = migrate(scope_dir / "assistant.db", PROJECT_MIGRATIONS)
    index_note(db, path)
    db.close()

    main(["deliver", "--provider", "claude-code"])
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "written"
    assert (repo / "CLAUDE.md").is_file()

    main(["deliver", "--provider", "claude-code", "--check"])
    assert json.loads(capsys.readouterr().out)["status"] == "clean"

    # drift then refused exit code
    target = repo / "CLAUDE.md"
    target.write_text(target.read_text().replace("Deploys freeze", "Deploys never freeze"))
    with pytest.raises(SystemExit) as err:
        main(["deliver", "--provider", "claude-code"])
    assert err.value.code == 1
    assert json.loads(capsys.readouterr().out)["status"] == "refused"

    main(["deliver", "--provider", "claude-code", "--force"])
    assert json.loads(capsys.readouterr().out)["status"] == "written"

    (repo / "CLAUDE.md").write_text("# Habits\n\nI review PRs on my phone.\n")
    main(["import", "--provider", "claude-code"])
    out = json.loads(capsys.readouterr().out)
    assert len(out["created"]) == 1

    with pytest.raises(ValueError, match="ZEE-57"):
        main(["deliver", "--provider", "pi"])
