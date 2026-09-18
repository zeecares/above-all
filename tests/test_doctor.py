"""First-run doctor and operational status, exercised through the CLI."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

CLI = [sys.executable, "-m", "above_all.cli"]


class World:
    def __init__(self, tmp_path: Path):
        self.home = tmp_path / "global-home"
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True, capture_output=True)
        self.env = dict(os.environ, ABOVE_ALL_HOME=str(self.home))
        self.scope = self.repo / ".above-all"

    def cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [*CLI, *args], env=self.env, cwd=self.repo,
            capture_output=True, text=True, timeout=120, check=False,
        )
        if check and result.returncode != 0:
            raise AssertionError(f"above-all {' '.join(args)} failed:\n{result.stderr}")
        return result


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def _configure_real(world: World) -> None:
    fake = world.repo / "fake_agent.py"
    fake.write_text("import sys; sys.exit(0)\n", encoding="utf-8")
    (world.home / "agent.toml").write_text(
        "[agent_cli]\n"
        'provider = "claude_code"\n'
        f'headless_command = ["{sys.executable}", "{fake}"]\n'
        f'interactive_command = ["{sys.executable}", "{fake}"]\n',
        encoding="utf-8",
    )
    (world.home / "routing.toml").write_text(
        "[models.free]\n"
        'endpoint = "https://gateway.internal.corp/v1"\n'
        'model = "free-model"\n'
        "[models.frontier]\n"
        'endpoint = "https://frontier.internal.corp/v1"\n'
        'model = "frontier-model"\n'
        "[levels.mechanical]\n"
        'tier = "free"\n'
        "[levels.routine]\n"
        'tier = "free"\n'
        "[levels.judgment]\n"
        'tier = "frontier"\n'
        "[levels.high_stakes]\n"
        'tier = "frontier"\n',
        encoding="utf-8",
    )


def test_doctor_before_init_gives_setup_path(world: World):
    result = world.cli("doctor", check=False)
    assert result.returncode == 1
    assert "above-all init" in result.stdout
    report = json.loads(world.cli("doctor", "--json", check=False).stdout)
    assert report["ok"] is False
    fails = {c["name"] for c in report["checks"] if c["status"] == "fail"}
    assert {"agent config", "routing", "global store"} <= fails
    assert all(c["fix"] for c in report["checks"] if c["status"] == "fail")


def test_doctor_flags_placeholder_config_after_init(world: World):
    world.cli("init")
    report = json.loads(world.cli("doctor", "--json", check=False).stdout)
    assert report["ok"] is False
    by_name = {}
    for check in report["checks"]:
        by_name.setdefault(check["name"], check)
    assert by_name["agent config"]["status"] == "fail"
    assert "placeholder" in by_name["agent config"]["detail"]
    assert by_name["routing"]["status"] == "fail"
    assert "placeholder" in by_name["routing"]["detail"]
    assert by_name["global store"]["status"] == "ok"
    assert by_name["privacy"]["status"] == "ok"
    # No secret material leaks: output never echoes command lines or endpoints.
    assert "your-agent-cli -p" not in json.dumps(report)


def test_doctor_healthy_once_configured(world: World):
    world.cli("init")
    _configure_real(world)
    result = world.cli("doctor")
    assert result.returncode == 0
    report = json.loads(world.cli("doctor", "--json").stdout)
    assert report["ok"] is True
    transcript = next(c for c in report["checks"] if c["name"] == "transcript capture")
    assert transcript["status"] == "ok"


def test_doctor_warns_on_unsupported_transcript_provider(world: World):
    world.cli("init")
    _configure_real(world)
    agent = (world.home / "agent.toml").read_text().replace("claude_code", "internal-build")
    (world.home / "agent.toml").write_text(agent, encoding="utf-8")
    report = json.loads(world.cli("doctor", "--json").stdout)
    transcript = next(c for c in report["checks"] if c["name"] == "transcript capture")
    assert transcript["status"] == "warn"
    assert "internal-build" in transcript["detail"]
    assert transcript["fix"]


def test_doctor_flags_privacy_violation(world: World):
    world.cli("init")
    _configure_real(world)
    tracked = world.scope / "notes" / "leak.md"
    tracked.write_text("secret\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(world.repo), "add", "-f", ".above-all/notes/leak.md"],
        check=True, capture_output=True,
    )
    report = json.loads(world.cli("doctor", "--json", check=False).stdout)
    privacy = next(c for c in report["checks"] if c["name"] == "privacy")
    assert privacy["status"] == "fail"
    assert "unsafe tracked" in privacy["detail"]


def test_doctor_is_read_only(world: World):
    world.cli("init")
    store = world.home / "assistant.db"
    before = store.read_bytes()
    world.cli("doctor", check=False)
    world.cli("status", check=False)
    assert store.read_bytes() == before


def test_status_dashboard_counts(world: World):
    world.cli("init")
    out = world.cli("status").stdout
    assert "outcomes: none" in out
    assert "watches: 0 total" in out
    report = json.loads(world.cli("status", "--json").stdout)
    assert report["initialized"] is True
    assert report["sessions"] == 0
    assert report["watches"] == {"total": 0, "due": 0, "next_fire_at": None}

    (world.scope / "candidates").mkdir(parents=True, exist_ok=True, )
    for name in ("c1", "c2"):
        (world.scope / "candidates" / f"{name}.md").write_text(
            "---\ntype: fact\nsources: [session:t]\ngenerated: 2026-09-18T00:00:00Z\n"
            f"verified: none\nstatus: candidate\nstale_after: null\n---\n# {name}\n\nbody\n",
            encoding="utf-8",
        )
    world.cli("watch", "create", "cadence", '{"minutes": 60}',
              "--next-fire", "2020-01-01T00:00:00+00:00")
    report = json.loads(world.cli("status", "--json").stdout)
    assert report["candidates"] == 2
    assert report["watches"]["total"] == 1
    assert report["watches"]["due"] == 1


def test_status_before_init_does_not_crash(world: World):
    result = world.cli("status")
    assert result.returncode == 0
    assert "init" in result.stdout


def test_doctor_handles_legacy_untracked_store(world: World):
    """A store from before schema_migrations tracking gets remediation, not a crash."""
    import sqlite3

    world.home.mkdir(parents=True)
    db = sqlite3.connect(world.home / "assistant.db")
    db.executescript(
        "CREATE TABLE outcomes (id TEXT PRIMARY KEY, project TEXT, title TEXT NOT NULL,"
        " status TEXT NOT NULL, owner TEXT NOT NULL, source_anchor TEXT,"
        " created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, provider TEXT NOT NULL);"
    )
    db.commit()
    db.close()
    result = world.cli("doctor", "--json", check=False)
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    report = json.loads(result.stdout)
    store = next(c for c in report["checks"] if c["name"] == "global store")
    assert store["status"] == "fail"
    assert "predates" in store["detail"]
    status = world.cli("status", "--json", check=False)
    assert "Traceback" not in status.stderr



def test_doctor_rejects_missing_endpoint_or_model(world: World):
    world.cli("init")
    _configure_real(world)
    routing = (world.home / "routing.toml").read_text()
    (world.home / "routing.toml").write_text(
        routing.replace('endpoint = "https://gateway.internal.corp/v1"', 'endpoint = ""'),
        encoding="utf-8",
    )
    report = json.loads(world.cli("doctor", "--json", check=False).stdout)
    check = next(c for c in report["checks"] if c["name"] == "routing")
    assert check["status"] == "fail"
    assert "missing or placeholder" in check["detail"]


def test_status_handles_empty_legacy_store_without_crashing(world: World):
    import sqlite3

    world.scope.mkdir(parents=True)
    sqlite3.connect(world.scope / "assistant.db").close()
    result = world.cli("status", "--json", check=False)
    assert result.returncode == 0
    assert "Traceback" not in result.stderr
    report = json.loads(result.stdout)
    assert report["sessions"] == 0
    assert report["tokens_in"] == 0
    assert report["tokens_out"] == 0
