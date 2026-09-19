"""Cross-weekend end-to-end suite: the full lifecycle through the installed CLI.

Every test drives the real ``above-all`` entry point as a subprocess inside a
temporary global home (ABOVE_ALL_HOME) and a temporary Git project, so module
boundaries - CLI, scopes, migrations, write gate, dispatch, harvest,
consolidation, watches - are exercised the way a user hits them.
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

CLI = [sys.executable, "-m", "above_all.cli"]

FAKE_AGENT = """
import json, os, sys
session_dir = os.environ.get("ABOVE_ALL_SESSION_DIR")
if session_dir:
    rows = [
        {"type": "user", "uuid": "u1", "timestamp": "2026-09-18T00:00:00Z",
         "message": {"content": "do the thing"}},
        {"type": "assistant", "uuid": "a1", "timestamp": "2026-09-18T00:00:01Z",
         "message": {"content": [{"type": "text", "text": "done"}],
                     "usage": {"input_tokens": 10, "output_tokens": 5}}},
    ]
    with open(os.path.join(session_dir, "transcript.jsonl"), "w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\\n")
sys.exit(0)
"""

# Historical weekend-4 era global schema (v1 core, v2 watches), pre-overlay.
_WEEKEND4_V1 = """CREATE TABLE outcomes (id TEXT PRIMARY KEY, project TEXT, title TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','running','blocked','done','cancelled')), owner TEXT NOT NULL, source_anchor TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE sessions (id TEXT PRIMARY KEY, provider TEXT NOT NULL, project TEXT, mode TEXT NOT NULL, model TEXT, outcome_id TEXT, parent_session_id TEXT, started_at TEXT, ended_at TEXT, source_path TEXT, import_version TEXT, summary TEXT, tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL);
CREATE TABLE decisions (id TEXT PRIMARY KEY, ts TEXT NOT NULL, project TEXT, outcome_id TEXT, decision TEXT NOT NULL, rationale TEXT, source_anchor TEXT);
CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL, outcome_id TEXT, payload_json TEXT);
"""
_WEEKEND4_V2_WATCHES = """CREATE TABLE watches (id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('clock','cadence','event','deadline')), spec_json TEXT NOT NULL, next_fire_at TEXT, interruption_policy TEXT NOT NULL DEFAULT 'value-gated', created_at TEXT NOT NULL, identity TEXT NOT NULL UNIQUE);
CREATE TABLE proactive_events (id INTEGER PRIMARY KEY AUTOINCREMENT, watch_id TEXT, created_at TEXT NOT NULL, payload_json TEXT NOT NULL, value TEXT, status TEXT NOT NULL CHECK(status IN ('pending','surfaced','internal')) DEFAULT 'pending', FOREIGN KEY(watch_id) REFERENCES watches(id));
"""


class World:
    def __init__(self, tmp_path: Path):
        self.home = tmp_path / "global-home"
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True, capture_output=True)
        self.env = dict(os.environ, ABOVE_ALL_HOME=str(self.home))
        self.scope = self.repo / ".above-all"

    def cli(self, *args: str, check: bool = True, cwd: Path | None = None) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [*CLI, *args],
            env=self.env,
            cwd=cwd or self.repo,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if check and result.returncode != 0:
            raise AssertionError(f"above-all {' '.join(args)} failed:\n{result.stderr}")
        return result

    def init(self) -> None:
        self.cli("init")

    def global_db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.home / "assistant.db")
        db.row_factory = sqlite3.Row
        return db

    def project_db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.scope / "assistant.db")
        db.row_factory = sqlite3.Row
        return db

    def configure_fake_agent(self) -> Path:
        fake = self.repo / "fake_agent.py"
        fake.write_text(FAKE_AGENT, encoding="utf-8")
        (self.home / "agent.toml").write_text(
            "[agent_cli]\n"
            'provider = "claude_code"\n'
            f'headless_command = ["{sys.executable}", "{fake}"]\n'
            f'interactive_command = ["{sys.executable}", "{fake}"]\n',
            encoding="utf-8",
        )
        return fake

    def drop_candidate(self, name: str, body: str, status: str = "reviewed",
                       stale_after: str = "null") -> Path:
        path = self.scope / "candidates" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\ntype: fact\nsources: [session:test]\n"
            "generated: 2026-09-18T00:00:00Z\nverified: none\n"
            f"status: {status}\nstale_after: {stale_after}\n---\n# {name}\n\n{body}\n",
            encoding="utf-8",
        )
        return path


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def _wait_for(predicate, timeout: float = 20.0, interval: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_init_then_work_harvest_approve_context_trace(world: World):
    """init -> interactive session -> exit harvest -> approve via gate -> context."""
    world.init()
    assert (world.home / "assistant.db").is_file()
    assert (world.home / "routing.toml").is_file()
    assert (world.home / "agent.toml").is_file()
    assert (world.scope / ".gitignore").read_text() == "*\n!.gitignore\n"
    assert (world.scope / "assistant.db").is_file()
    assert (world.scope / "notes").is_dir()
    assert (world.scope / "candidates").is_dir()

    # A hand-added note is a draft; drafts must never enter generated context.
    world.cli("note", "add", "Draft fact", "unreviewed draft body", "--source", "file:readme")

    world.configure_fake_agent()
    result = world.cli("work")
    payload = json.loads(result.stdout)
    assert payload["status"] == "done" and payload["exit_code"] == 0

    # Exit harvest produced exactly one review candidate, no active-memory write.
    candidates = json.loads(world.cli("note", "candidates").stdout)
    assert len(candidates) == 1
    candidate_id = candidates[0]["id"]

    # Approve through the write gate: candidate becomes active, searchable memory.
    world.cli("note", "approve", candidate_id)
    found = json.loads(world.cli("note", "search", "interactive").stdout)
    assert [row["id"] for row in found] == [candidate_id]
    assert not (world.scope / "candidates" / f"{candidate_id}.md").exists()

    context_path = Path(world.cli("note", "context").stdout.strip())
    context = context_path.read_text()
    assert "interactive session" in context
    assert "_Source: project knowledge_" in context
    assert "unreviewed draft body" not in context

    # A global-scope note joins the overlay below project notes, each labeled.
    global_note = world.home / "notes" / "global1.md"
    global_note.parent.mkdir(parents=True, exist_ok=True)
    global_note.write_text(
        "---\ntype: fact\nsources: [file:global]\n"
        "generated: 2026-09-18T00:00:00Z\nverified: source\n"
        "status: active\nstale_after: null\n---\n# Global habit\n\n"
        "globally shared rehearsal pattern\n",
        encoding="utf-8",
    )
    from above_all.db import GLOBAL_MIGRATIONS, migrate
    from above_all.notes import index_note

    gdb = migrate(world.home / "assistant.db", GLOBAL_MIGRATIONS)
    index_note(gdb, global_note)
    gdb.close()
    context = Path(world.cli("note", "context").stdout.strip()).read_text()
    assert "globally shared rehearsal pattern" in context
    assert "_Source: global knowledge_" in context
    assert context.index("_Source: project knowledge_") < context.index(
        "_Source: global knowledge_"
    )

    # The fake provider's transcript was imported into the trace plane.
    db = world.global_db()
    imported = db.execute(
        "SELECT status FROM trace_imports WHERE status='imported'"
    ).fetchall()
    assert len(imported) == 1
    usage = db.execute("SELECT tokens_in, tokens_out FROM trace_usage").fetchone()
    assert (usage["tokens_in"], usage["tokens_out"]) == (10, 5)
    db.close()


def test_consolidation_watch_and_sweep_journey(world: World):
    """watch -> maintenance -> weekly proposal -> explicit apply -> expiry sweep."""
    world.init()
    world.drop_candidate("zephyr", "quarantine zephyr lattice vortex")
    world.cli(
        "watch", "create", "cadence", '{"minutes": 60}',
        "--next-fire", "2020-01-01T00:00:00+00:00",
    )

    weekly = json.loads(world.cli("maintenance", "--weekly").stdout)
    assert len(weekly["watches"]["fired"]) == 1
    # The value gate fails closed without a configured classifier.
    assert weekly["watches"]["gate"]["surfaced"] == []
    assert weekly["watches"]["gate"]["internal_count"] == 1

    changeset_path = Path(weekly["weekly_changeset"])
    payload = json.loads(changeset_path.read_text())
    assert payload["status"] == "proposed"
    assert payload["requires_explicit_approve"] is True
    promote = [c for c in payload["changes"] if c.get("action") == "promote"]
    assert [c["candidate_id"] for c in promote] == ["zephyr"]

    # Applying without --approve is refused; with it, the gate promotes.
    refused = world.cli("consolidate", "apply", str(changeset_path), check=False)
    assert refused.returncode != 0
    applied = json.loads(
        world.cli("consolidate", "apply", str(changeset_path), "--approve").stdout
    )
    assert applied["status"] == "applied"
    again = json.loads(
        world.cli("consolidate", "apply", str(changeset_path), "--approve").stdout
    )
    assert again["status"] == "applied" and again["idempotent"] is True
    db = world.project_db()
    assert db.execute(
        "SELECT status FROM notes WHERE id='zephyr'"
    ).fetchone()["status"] == "active"
    db.close()

    # A stale active note is swept to needs-review, not deleted.
    world.drop_candidate("fossil", "ancient sediment amber relic", stale_after="2020-01-01")
    world.cli("note", "approve", "fossil")
    swept = json.loads(world.cli("consolidate", "sweep").stdout)
    assert swept["expired"] == ["fossil"]
    text = (world.scope / "notes" / "fossil.md").read_text()
    assert "status: needs-review" in text
    assert (world.scope / "notes" / "fossil.md").is_file()


def test_dispatch_analyze_and_reconcile_journey(world: World):
    """headless do -> outcomes complete -> analysis proposes only with evidence."""
    world.init()
    world.configure_fake_agent()
    for index in range(3):
        world.cli("do", f"headless intent {index}")
    done = _wait_for(
        lambda: world.global_db()
        .execute("SELECT COUNT(*) c FROM outcomes WHERE status='done'")
        .fetchone()["c"]
        >= 3
    )
    assert done, "async outcomes did not complete"
    analysis = json.loads(world.cli("analyze").stdout)
    assert analysis["completed_outcomes"] == 3
    assert set(analysis["queues"]) == {"system", "user", "eval"}
    sessions = json.loads(world.cli("sessions").stdout)
    assert len(sessions) >= 3
    assert json.loads(world.cli("reconcile").stdout) == []
    routed = json.loads(world.cli("route", "judgment").stdout)
    assert routed["tier"] == "frontier"


def test_concurrent_approve_has_exactly_one_winner(world: World):
    world.init()
    world.drop_candidate("contended", "rare simultaneous promotion target", status="candidate")
    procs = [
        subprocess.Popen(
            [*CLI, "note", "approve", "contended"],
            env=world.env, cwd=world.repo,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(2)
    ]
    codes = [proc.wait(timeout=60) for proc in procs]
    assert sorted(codes) == [0, 1]
    db = world.project_db()
    active = db.execute(
        "SELECT COUNT(*) c FROM notes WHERE id='contended' AND status='active'"
    ).fetchone()["c"]
    db.close()
    assert active == 1
    assert not (world.scope / "candidates" / "contended.md").exists()


def test_reconcile_after_worker_kill_blocks_outcome_and_reaps_child(world: World):
    world.init()
    out = json.loads(world.cli("do", "sleepy intent", "--",
                               sys.executable, "-c", "import time; time.sleep(120)").stdout)
    session_dir = Path(out["session_dir"])
    child_pid_path = session_dir / "child.pid"
    assert _wait_for(child_pid_path.is_file, timeout=15), "worker never started its child"
    child_pid = int(child_pid_path.read_text())
    worker_pid = int((session_dir / "worker.pid").read_text())

    # Kill only the worker; its child survives inside the worker's process group.
    os.kill(worker_pid, signal.SIGKILL)
    reconciled = json.loads(world.cli("reconcile").stdout)
    assert reconciled == [
        {"session_id": out["session_id"], "outcome_id": out["outcome_id"], "status": "blocked"}
    ]
    db = world.global_db()
    status = db.execute(
        "SELECT status FROM outcomes WHERE id=?", (out["outcome_id"],)
    ).fetchone()["status"]
    db.close()
    assert status == "blocked"
    assert json.loads((session_dir / "result.json").read_text())["status"] == "blocked"
    # The orphaned child was terminated as part of the worker's process group.
    assert _wait_for(
        lambda: _process_gone(child_pid), timeout=10
    ), "orphaned child survived reconciliation"


def _process_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return True
    return False


def test_cli_migrates_historical_global_store(world: World):
    """A weekend-4 era global store (v2 = watches) upgrades through the CLI."""
    world.home.mkdir(parents=True)
    db = sqlite3.connect(world.home / "assistant.db")
    db.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY,"
        " applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    for version, sql in ((1, _WEEKEND4_V1), (2, _WEEKEND4_V2_WATCHES)):
        db.executescript(sql)
        db.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
    db.execute(
        "INSERT INTO watches (id, kind, spec_json, created_at, identity)"
        " VALUES ('w1', 'cadence', '{\"minutes\": 60}', '2026-09-17T00:00:00+00:00', 'w1')"
    )
    db.commit()
    db.close()

    world.init()
    upgraded = world.global_db()
    versions = {r[0] for r in upgraded.execute("SELECT version FROM schema_migrations")}
    assert versions == {1, 2, 3, 4}
    assert upgraded.execute("SELECT id FROM watches").fetchone()["id"] == "w1"
    upgraded.execute("SELECT COUNT(*) FROM notes").fetchone()
    upgraded.close()


def test_malformed_agent_config_fails_loudly(world: World):
    world.init()
    (world.home / "agent.toml").write_text("this is = not = toml\n", encoding="utf-8")
    result = world.cli("work", check=False)
    assert result.returncode != 0
    assert "toml" in result.stderr.lower()



def test_wheel_install_init_includes_example_configs(tmp_path: Path):
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "-w", str(wheel_dir)],
        check=True, capture_output=True, text=True,
    )
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    wheel = next(wheel_dir.glob("above_all-*.whl"))
    subprocess.run([str(python), "-m", "pip", "install", str(wheel)], check=True, capture_output=True)
    home = tmp_path / "installed-home"
    repo = tmp_path / "installed-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    env = dict(os.environ, ABOVE_ALL_HOME=str(home))
    result = subprocess.run(
        [str(python), "-m", "above_all.cli", "init"], cwd=repo, env=env,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (home / "agent.toml").is_file()
    assert (home / "routing.toml").is_file()
