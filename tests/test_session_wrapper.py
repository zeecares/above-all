import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from above_all.db import GLOBAL_MIGRATIONS, PROJECT_MIGRATIONS, migrate
from above_all.memory_backend import get_backend
from above_all.notes import create_note, index_note, list_candidates, set_note_status
from above_all.paths import resolve_scopes
from above_all.session_wrapper import dispatch_headless, wrap_interactive


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    monkeypatch.setenv("ABOVE_ALL_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(root)
    scopes = resolve_scopes(root)
    migrate(scopes.global_root / "assistant.db", GLOBAL_MIGRATIONS).close()
    migrate(scopes.project_root / "assistant.db", PROJECT_MIGRATIONS).close()
    return scopes


def active_note(scopes, title="Project convention", body="use pytest tmp_path"):
    path = create_note(scopes.project_root / "notes", title, body, "fact", ["file:test"])
    set_note_status(path, "active")
    db = migrate(scopes.project_root / "assistant.db", PROJECT_MIGRATIONS)
    index_note(db, path)
    db.close()


def session_rows(db_path, session_id):
    db = sqlite3.connect(db_path)
    rows = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchall()
    db.close()
    return rows


def test_headless_dispatch_records_session_in_both_scopes(repo):
    result = dispatch_headless(
        repo, [sys.executable, "-c", "print('ok')"], "tidy the tests", get_backend()
    )
    assert result.status == "done" and result.exit_code == 0
    assert session_rows(repo.global_root / "assistant.db", result.session_id)
    assert session_rows(repo.project_root / "assistant.db", result.session_id)


def test_handoff_envelope_and_prompt_are_written(repo):
    result = dispatch_headless(
        repo,
        [sys.executable, "-c", "pass"],
        "raw intent here",
        get_backend(),
        constraints=["no network"],
        source_anchors=["file:app.py"],
    )
    envelope = json.loads((result.session_dir / "envelope.json").read_text())
    assert envelope["intent"] == "raw intent here"  # raw intent, not a paraphrase
    assert envelope["constraints"] == ["no network"]
    assert envelope["return"] == ["outcome", "evidence", "changes", "blockers"]
    assert "raw intent here" in (result.session_dir / "prompt.md").read_text()


def test_child_process_receives_session_env(repo, tmp_path):
    marker = tmp_path / "env.json"
    script = (
        "import json,os,pathlib;"
        f"pathlib.Path(r'{marker}').write_text(json.dumps({{k:v for k,v in os.environ.items() "
        "if k.startswith('ABOVE_ALL')}))"
    )
    result = dispatch_headless(repo, [sys.executable, "-c", script], "check env", get_backend())
    env = json.loads(marker.read_text())
    assert env["ABOVE_ALL_SESSION_ID"] == result.session_id
    assert env["ABOVE_ALL_PROMPT"].endswith("prompt.md")


def test_result_and_diff_are_captured(repo):
    script = "import pathlib;pathlib.Path('new.txt').write_text('x')"
    result = dispatch_headless(repo, [sys.executable, "-c", script], "make a file", get_backend())
    payload = json.loads((result.session_dir / "result.json").read_text())
    assert payload["status"] == "done"
    assert any("new.txt" in line for line in payload["changed_paths"])
    assert (result.session_dir / "diff.patch").is_file()


def test_failing_command_records_failed_not_lost(repo):
    result = dispatch_headless(
        repo, [sys.executable, "-c", "import sys;sys.exit(3)"], "will fail", get_backend()
    )
    assert result.status == "failed" and result.exit_code == 3
    assert session_rows(repo.global_root / "assistant.db", result.session_id)
    candidates = list_candidates(repo.project_root / "candidates")
    assert candidates == []  # no transcript fact: process boilerplate is suppressed


def test_exit_creates_candidate_never_active_memory(repo):
    dispatch_headless(repo, [sys.executable, "-c", "pass"], "harvest me", get_backend())
    candidates = list_candidates(repo.project_root / "candidates")
    assert candidates == []  # no useful trace, so no memory pollution
    db = migrate(repo.project_root / "assistant.db", PROJECT_MIGRATIONS)
    assert get_backend().search(db, "harvest") == []  # never active directly
    db.close()


def test_outcome_row_reflects_exit(repo):
    result = dispatch_headless(repo, [sys.executable, "-c", "pass"], "ship it", get_backend())
    db = sqlite3.connect(repo.project_root / "assistant.db")
    row = db.execute("SELECT status,owner FROM outcomes WHERE title='ship it'").fetchone()
    db.close()
    assert row[0] == "done" and row[1] == f"session:{result.session_id}"


def test_interactive_wrap_preloads_context_and_records(repo):
    active_note(repo)
    result = wrap_interactive(repo, [sys.executable, "-c", "pass"], get_backend())
    assert result.mode == "interactive" and result.status == "done"
    context = (repo.project_root / "AGENT_CONTEXT.md").read_text()
    assert "Project convention" in context  # approved notes preloaded
    db = sqlite3.connect(repo.project_root / "assistant.db")
    mode = db.execute("SELECT mode FROM sessions WHERE id=?", (result.session_id,)).fetchone()[0]
    db.close()
    assert mode == "interactive"


def test_interactive_skill_context_is_session_scoped_and_absent_when_unselected(repo, tmp_path):
    marker = tmp_path / "skills.txt"
    script = (
        "import os,pathlib;"
        f"p=os.environ.get('ABOVE_ALL_SKILLS');pathlib.Path(r'{marker}').write_text(pathlib.Path(p).read_text() if p else 'NONE')"
    )
    first = wrap_interactive(
        repo, [sys.executable, "-c", script], get_backend(), skill_text="# pr\n\nUse it."
    )
    assert (first.session_dir / "SELECTED_SKILLS.md").is_file()
    assert marker.read_text() == "# pr\n\nUse it.\n"
    second = wrap_interactive(repo, [sys.executable, "-c", script], get_backend())
    assert marker.read_text() == "NONE"
    assert not (second.session_dir / "SELECTED_SKILLS.md").exists()
    assert not (repo.project_root / "SELECTED_SKILLS.md").exists()


def test_context_excludes_candidates(repo):
    backend = get_backend()
    backend.create_candidate(
        repo.project_root,
        title="Unreviewed",
        body="drafty",
        note_type="fact",
        sources=["session:s1"],
    )
    wrap_interactive(repo, [sys.executable, "-c", "pass"], backend)
    assert "Unreviewed" not in (repo.project_root / "AGENT_CONTEXT.md").read_text()


def test_async_accepts_before_child_finishes_and_streams_large_logs(repo):
    import time

    from above_all.async_outcomes import launch_async

    accepted = launch_async(
        repo, [sys.executable, "-c", "import time; print('x'*2000000); time.sleep(1)"], "long run"
    )
    assert accepted.status == "accepted"
    db = sqlite3.connect(repo.global_root / "assistant.db")
    assert db.execute("SELECT status FROM outcomes WHERE id=?", (accepted.outcome_id,)).fetchone()[
        0
    ] in {"pending", "running"}
    db.close()
    deadline = time.time() + 5
    while time.time() < deadline and not (accepted.session_dir / "result.json").exists():
        time.sleep(0.05)
    assert (accepted.session_dir / "stdout.log").stat().st_size >= 2_000_000


def test_missing_executable_becomes_blocked_with_evidence(repo):
    import time

    from above_all.async_outcomes import launch_async

    accepted = launch_async(repo, ["definitely-not-an-executable"], "cannot launch")
    deadline = time.time() + 5
    while time.time() < deadline and not (accepted.session_dir / "result.json").exists():
        time.sleep(0.05)
    result = json.loads((accepted.session_dir / "result.json").read_text())
    assert result["status"] == "blocked" and "launch failed" in result["summary"]


def test_reconcile_marks_killed_wrapper_blocked_and_preserves_logs(repo):
    from above_all.async_outcomes import reconcile

    session = repo.project_root / "sessions" / "dead"
    session.mkdir(parents=True)
    (session / "stdout.log").write_text("partial output")
    manifest = {
        "session_id": "dead",
        "outcome_id": "out-dead",
        "intent": "dead run",
        "command": [],
        "provider": "test",
        "session_dir": str(session),
        "started_at": "2026-09-18T00:00:00+00:00",
        "scopes": [
            [
                str(repo.global_root / "assistant.db"),
                GLOBAL_MIGRATIONS,
                repo.project_root.parent.name,
            ],
            [str(repo.project_root / "assistant.db"), PROJECT_MIGRATIONS, None],
        ],
    }
    (session / "worker.json").write_text(json.dumps(manifest))
    (session / "worker.pid").write_text("99999999")
    for path, migrations, project in manifest["scopes"]:
        db = migrate(Path(path), migrations)
        if project:
            db.execute(
                "INSERT INTO outcomes VALUES (?,?,?,?,?,?,?,?)",
                (
                    "out-dead",
                    project,
                    "dead run",
                    "running",
                    "session:dead",
                    "session:dead",
                    "x",
                    "x",
                ),
            )
        else:
            db.execute(
                "INSERT INTO outcomes VALUES (?,?,?,?,?,?,?)",
                ("out-dead", "dead run", "running", "session:dead", "session:dead", "x", "x"),
            )
        db.execute(
            "INSERT INTO sessions(id,provider,mode,outcome_id,started_at,source_path) VALUES (?,?,?,?,?,?)",
            ("dead", "test", "headless", "out-dead", "x", str(session)),
        )
        db.commit()
        db.close()
    assert reconcile(repo)[0]["status"] == "blocked"
    assert (session / "stdout.log").read_text() == "partial output"


def test_reconcile_does_not_race_pending_launch_without_pid(repo):
    from above_all.async_outcomes import reconcile

    session = repo.project_root / "sessions" / "launching"
    session.mkdir(parents=True)
    manifest = {
        "session_id": "launching",
        "outcome_id": "out-launching",
        "intent": "launching",
        "command": [],
        "provider": "test",
        "session_dir": str(session),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "scopes": [
            [
                str(repo.global_root / "assistant.db"),
                GLOBAL_MIGRATIONS,
                repo.project_root.parent.name,
            ],
            [str(repo.project_root / "assistant.db"), PROJECT_MIGRATIONS, None],
        ],
    }
    (session / "worker.json").write_text(json.dumps(manifest))
    for path, migrations, project in manifest["scopes"]:
        db = migrate(Path(path), migrations)
        if project:
            db.execute(
                "INSERT INTO outcomes VALUES (?,?,?,?,?,?,?,?)",
                (
                    "out-launching",
                    project,
                    "launching",
                    "pending",
                    "session:launching",
                    "session:launching",
                    "x",
                    "x",
                ),
            )
        else:
            db.execute(
                "INSERT INTO outcomes VALUES (?,?,?,?,?,?,?)",
                (
                    "out-launching",
                    "launching",
                    "pending",
                    "session:launching",
                    "session:launching",
                    "x",
                    "x",
                ),
            )
        db.execute(
            "INSERT INTO sessions(id,provider,mode,outcome_id,started_at,source_path) VALUES (?,?,?,?,?,?)",
            ("launching", "test", "headless", "out-launching", "x", str(session)),
        )
        db.commit()
        db.close()
    assert reconcile(repo) == []
    db = sqlite3.connect(repo.global_root / "assistant.db")
    assert (
        db.execute("SELECT status FROM outcomes WHERE id='out-launching'").fetchone()[0]
        == "pending"
    )


def test_finish_updates_global_control_plane_last(repo, monkeypatch):
    from above_all import async_outcomes

    session = repo.project_root / "sessions" / "ordering"
    session.mkdir(parents=True)
    manifest = {
        "session_id": "ordering",
        "outcome_id": "out-ordering",
        "intent": "ordering",
        "command": [],
        "provider": "test",
        "session_dir": str(session),
        "started_at": "2026-09-18T00:00:00+00:00",
        "scopes": [
            [
                str(repo.global_root / "assistant.db"),
                GLOBAL_MIGRATIONS,
                repo.project_root.parent.name,
            ],
            [str(repo.project_root / "assistant.db"), PROJECT_MIGRATIONS, None],
        ],
    }
    seen = []
    real_migrate = async_outcomes.migrate

    def recording_migrate(path, migrations):
        seen.append(Path(path))
        return real_migrate(Path(path), migrations)

    monkeypatch.setattr(async_outcomes, "migrate", recording_migrate)
    async_outcomes._finish(manifest, "blocked", "proof")
    assert seen == [repo.project_root / "assistant.db", repo.global_root / "assistant.db"]
def test_project_session_overlays_global_notes_with_provenance(repo):
    global_db = migrate(repo.global_root / "assistant.db", GLOBAL_MIGRATIONS)
    global_note = create_note(
        repo.global_root / "notes",
        "Global preference",
        "use concise output",
        "preference",
        ["user:test"],
    )
    set_note_status(global_note, "active")
    index_note(global_db, global_note)
    project_db = migrate(repo.project_root / "assistant.db", PROJECT_MIGRATIONS)
    project_note = create_note(
        repo.project_root / "notes", "Project rule", "run local tests", "procedure", ["file:test"]
    )
    set_note_status(project_note, "active")
    index_note(project_db, project_note)
    global_db.close()
    project_db.close()

    result = dispatch_headless(repo, [sys.executable, "-c", "pass"], "overlay", get_backend())
    prompt = (result.session_dir / "prompt.md").read_text()
    assert "Global preference" in prompt and "Project rule" in prompt


def test_project_note_id_shadows_global_without_cross_project_leak(repo):
    from above_all.agent_context import generate_agent_context

    global_db = migrate(repo.global_root / "assistant.db", GLOBAL_MIGRATIONS)
    global_note = create_note(
        repo.global_root / "notes", "Global", "global body", "fact", ["user:test"]
    )
    set_note_status(global_note, "active")
    index_note(global_db, global_note)
    project_db = migrate(repo.project_root / "assistant.db", PROJECT_MIGRATIONS)
    shadow = repo.project_root / "notes" / global_note.name
    shadow.parent.mkdir(exist_ok=True)
    shadow.write_text(
        global_note.read_text()
        .replace("# Global", "# Project")
        .replace("global body", "project body")
    )
    index_note(project_db, shadow)
    context = generate_agent_context(
        repo.project_root, project_db, get_backend(), global_db
    ).read_text()
    assert "project body" in context and "global body" not in context
    assert "Source: project knowledge" in context


def test_project_notes_cannot_be_starved_by_global_limit(repo):
    from above_all.agent_context import merged_active_notes

    global_db = migrate(repo.global_root / "assistant.db", GLOBAL_MIGRATIONS)
    for index in range(10):
        path = create_note(
            repo.global_root / "notes", f"Global {index}", f"global {index}", "fact", ["user:test"]
        )
        set_note_status(path, "active")
        index_note(global_db, path)
    project_db = migrate(repo.project_root / "assistant.db", PROJECT_MIGRATIONS)
    project = create_note(
        repo.project_root / "notes", "Project wins", "project body", "fact", ["file:test"]
    )
    set_note_status(project, "active")
    index_note(project_db, project)
    merged = merged_active_notes(global_db, project_db, get_backend(), limit=5)
    assert merged[0]["id"] == project.stem
    assert len(merged) == 5


def test_headless_envelope_preserves_scope_provenance(repo):
    global_db = migrate(repo.global_root / "assistant.db", GLOBAL_MIGRATIONS)
    path = create_note(
        repo.global_root / "notes", "Global fact", "global body", "fact", ["user:test"]
    )
    set_note_status(path, "active")
    index_note(global_db, path)
    global_db.close()
    active_note(repo, title="Project fact", body="project body")
    result = dispatch_headless(repo, [sys.executable, "-c", "pass"], "provenance", get_backend())
    prompt = (result.session_dir / "prompt.md").read_text()
    envelope = json.loads((result.session_dir / "envelope.json").read_text())
    assert "Source: project knowledge" in prompt and "Source: global knowledge" in prompt
    assert {item["scope"] for item in envelope["relevant_notes"]} == {"global", "project"}


