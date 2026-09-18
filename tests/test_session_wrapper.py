import json
import sqlite3
import subprocess
import sys

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
        repo, [sys.executable, "-c", "pass"], "raw intent here",
        get_backend(), constraints=["no network"], source_anchors=["file:app.py"],
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
    assert len(candidates) == 1  # candidate created even on failure


def test_exit_creates_candidate_never_active_memory(repo):
    dispatch_headless(repo, [sys.executable, "-c", "pass"], "harvest me", get_backend())
    candidates = list_candidates(repo.project_root / "candidates")
    assert len(candidates) == 1
    db = migrate(repo.project_root / "assistant.db", PROJECT_MIGRATIONS)
    assert get_backend().search(db, "harvest") == []  # never active directly
    db.close()


def test_outcome_row_reflects_exit(repo):
    result = dispatch_headless(repo, [sys.executable, "-c", "pass"], "ship it", get_backend())
    db = sqlite3.connect(repo.project_root / "assistant.db")
    row = db.execute(
        "SELECT status,owner FROM outcomes WHERE title='ship it'"
    ).fetchone()
    db.close()
    assert row[0] == "done" and row[1] == f"session:{result.session_id}"


def test_interactive_wrap_preloads_context_and_records(repo):
    active_note(repo)
    result = wrap_interactive(repo, [sys.executable, "-c", "pass"], get_backend())
    assert result.mode == "interactive" and result.status == "done"
    context = (repo.project_root / "AGENT_CONTEXT.md").read_text()
    assert "Project convention" in context  # approved notes preloaded
    db = sqlite3.connect(repo.project_root / "assistant.db")
    mode = db.execute(
        "SELECT mode FROM sessions WHERE id=?", (result.session_id,)
    ).fetchone()[0]
    db.close()
    assert mode == "interactive"


def test_interactive_skill_context_is_session_scoped_and_absent_when_unselected(repo, tmp_path):
    marker = tmp_path / "skills.txt"
    script = (
        "import os,pathlib;"
        f"p=os.environ.get('ABOVE_ALL_SKILLS');pathlib.Path(r'{marker}').write_text(pathlib.Path(p).read_text() if p else 'NONE')"
    )
    first = wrap_interactive(repo, [sys.executable, "-c", script], get_backend(), skill_text="# pr\n\nUse it.")
    assert (first.session_dir / "SELECTED_SKILLS.md").is_file()
    assert marker.read_text() == "# pr\n\nUse it.\n"
    second = wrap_interactive(repo, [sys.executable, "-c", script], get_backend())
    assert marker.read_text() == "NONE"
    assert not (second.session_dir / "SELECTED_SKILLS.md").exists()
    assert not (repo.project_root / "SELECTED_SKILLS.md").exists()


def test_context_excludes_candidates(repo):
    backend = get_backend()
    backend.create_candidate(
        repo.project_root, title="Unreviewed", body="drafty", note_type="fact",
        sources=["session:s1"],
    )
    wrap_interactive(repo, [sys.executable, "-c", "pass"], backend)
    assert "Unreviewed" not in (repo.project_root / "AGENT_CONTEXT.md").read_text()



def test_project_session_overlays_global_notes_with_provenance(repo):
    global_db = migrate(repo.global_root / "assistant.db", GLOBAL_MIGRATIONS)
    global_note = create_note(repo.global_root / "notes", "Global preference", "use concise output", "preference", ["user:test"])
    set_note_status(global_note, "active"); index_note(global_db, global_note)
    project_db = migrate(repo.project_root / "assistant.db", PROJECT_MIGRATIONS)
    project_note = create_note(repo.project_root / "notes", "Project rule", "run local tests", "procedure", ["file:test"])
    set_note_status(project_note, "active"); index_note(project_db, project_note)
    global_db.close(); project_db.close()

    result = dispatch_headless(repo, [sys.executable, "-c", "pass"], "overlay", get_backend())
    prompt = (result.session_dir / "prompt.md").read_text()
    assert "Global preference" in prompt and "Project rule" in prompt


def test_project_note_id_shadows_global_without_cross_project_leak(repo):
    from above_all.agent_context import generate_agent_context

    global_db = migrate(repo.global_root / "assistant.db", GLOBAL_MIGRATIONS)
    global_note = create_note(repo.global_root / "notes", "Global", "global body", "fact", ["user:test"])
    set_note_status(global_note, "active"); index_note(global_db, global_note)
    project_db = migrate(repo.project_root / "assistant.db", PROJECT_MIGRATIONS)
    shadow = repo.project_root / "notes" / global_note.name
    shadow.parent.mkdir(exist_ok=True); shadow.write_text(global_note.read_text().replace("# Global", "# Project").replace("global body", "project body"))
    index_note(project_db, shadow)
    context = generate_agent_context(repo.project_root, project_db, get_backend(), global_db).read_text()
    assert "project body" in context and "global body" not in context
    assert "Source: project knowledge" in context
