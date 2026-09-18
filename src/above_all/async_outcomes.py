"""Durable asynchronous headless outcomes and crash reconciliation."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .db import GLOBAL_MIGRATIONS, PROJECT_MIGRATIONS, migrate
from .paths import Scopes


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class AcceptedOutcome:
    session_id: str
    outcome_id: str
    session_dir: Path
    pid: int
    status: str = "accepted"


def _scope_specs(scopes: Scopes) -> list[tuple[str, list[str], str | None]]:
    specs = [
        (
            str(scopes.global_root / "assistant.db"),
            GLOBAL_MIGRATIONS,
            scopes.project_root.parent.name if scopes.project_root else None,
        )
    ]
    if scopes.project_root:
        specs.append((str(scopes.project_root / "assistant.db"), PROJECT_MIGRATIONS, None))
    return specs


def _upsert(db: sqlite3.Connection, project: str | None, outcome: dict, session: dict) -> None:
    now = _now()
    with db:
        if project is not None:
            db.execute(
                "INSERT OR REPLACE INTO outcomes (id,project,title,status,owner,source_anchor,created_at,updated_at) VALUES (?,?,?,?,?,?,COALESCE((SELECT created_at FROM outcomes WHERE id=?),?),?)",
                (
                    outcome["id"],
                    project,
                    outcome["title"],
                    outcome["status"],
                    outcome["owner"],
                    outcome["source_anchor"],
                    outcome["id"],
                    now,
                    now,
                ),
            )
            db.execute(
                "INSERT OR REPLACE INTO sessions (id,provider,project,mode,outcome_id,started_at,ended_at,source_path,summary) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    session["id"],
                    session["provider"],
                    project,
                    "headless",
                    outcome["id"],
                    session["started_at"],
                    session.get("ended_at"),
                    session["source_path"],
                    session.get("summary"),
                ),
            )
        else:
            db.execute(
                "INSERT OR REPLACE INTO outcomes (id,title,status,owner,source_anchor,created_at,updated_at) VALUES (?,?,?,?,?,COALESCE((SELECT created_at FROM outcomes WHERE id=?),?),?)",
                (
                    outcome["id"],
                    outcome["title"],
                    outcome["status"],
                    outcome["owner"],
                    outcome["source_anchor"],
                    outcome["id"],
                    now,
                    now,
                ),
            )
            db.execute(
                "INSERT OR REPLACE INTO sessions (id,provider,mode,outcome_id,started_at,ended_at,source_path,summary) VALUES (?,?,?,?,?,?,?,?)",
                (
                    session["id"],
                    session["provider"],
                    "headless",
                    outcome["id"],
                    session["started_at"],
                    session.get("ended_at"),
                    session["source_path"],
                    session.get("summary"),
                ),
            )
        db.execute(
            "INSERT INTO events(ts,kind,outcome_id,payload_json) VALUES (?,?,?,?)",
            (
                now,
                f"outcome_{outcome['status']}",
                outcome["id"],
                json.dumps({"session_id": session["id"], "summary": session.get("summary")}),
            ),
        )


def launch_async(
    scopes: Scopes,
    command: list[str],
    intent: str,
    provider: str = "unknown",
    outcome_id: str | None = None,
) -> AcceptedOutcome:
    """Persist acceptance before spawning a detached worker and return immediately."""
    session_id = uuid4().hex[:12]
    outcome_id = outcome_id or f"out-{uuid4().hex[:8]}"
    session_dir = (scopes.project_root or scopes.global_root) / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "session_id": session_id,
        "outcome_id": outcome_id,
        "intent": intent,
        "command": command,
        "provider": provider,
        "session_dir": str(session_dir),
        "scopes": _scope_specs(scopes),
        "started_at": _now(),
    }
    manifest_path = session_dir / "worker.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    outcome = {
        "id": outcome_id,
        "title": intent,
        "status": "pending",
        "owner": f"session:{session_id}",
        "source_anchor": f"session:{session_id}",
    }
    session = {
        "id": session_id,
        "provider": provider,
        "started_at": manifest["started_at"],
        "source_path": str(session_dir),
    }
    for db_path, migrations, project in manifest["scopes"]:
        db = migrate(Path(db_path), migrations)
        _upsert(db, project, outcome, session)
        db.close()
    try:
        worker = subprocess.Popen(
            [sys.executable, "-m", "above_all.async_outcomes", str(manifest_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        _finish(manifest, "blocked", f"worker launch failed: {exc}")
        raise
    (session_dir / "worker.pid").write_text(f"{worker.pid}\n")
    return AcceptedOutcome(session_id, outcome_id, session_dir, worker.pid)


def _finish(manifest: dict, status: str, summary: str) -> None:
    outcome = {
        "id": manifest["outcome_id"],
        "title": manifest["intent"],
        "status": status,
        "owner": f"session:{manifest['session_id']}",
        "source_anchor": f"session:{manifest['session_id']}",
    }
    session = {
        "id": manifest["session_id"],
        "provider": manifest["provider"],
        "started_at": manifest["started_at"],
        "ended_at": _now(),
        "source_path": manifest["session_dir"],
        "summary": summary,
    }
    # Update project mirrors first and the global control-plane row last. If the
    # process dies between stores, global remains pending/running so reconcile
    # retries the idempotent finish and repairs every mirror.
    for db_path, migrations, project in reversed(manifest["scopes"]):
        db = migrate(Path(db_path), migrations)
        _upsert(db, project, outcome, session)
        db.close()
    Path(manifest["session_dir"], "result.json").write_text(
        json.dumps({"status": status, "summary": summary}, indent=2) + "\n"
    )


def run_worker(manifest_path: Path) -> int:
    manifest = json.loads(manifest_path.read_text())
    session_dir = Path(manifest["session_dir"])
    outcome = {
        "id": manifest["outcome_id"],
        "title": manifest["intent"],
        "status": "running",
        "owner": f"session:{manifest['session_id']}",
        "source_anchor": f"session:{manifest['session_id']}",
    }
    session = {
        "id": manifest["session_id"],
        "provider": manifest["provider"],
        "started_at": manifest["started_at"],
        "source_path": manifest["session_dir"],
    }
    for db_path, migrations, project in manifest["scopes"]:
        db = migrate(Path(db_path), migrations)
        _upsert(db, project, outcome, session)
        db.close()
    env = dict(
        os.environ,
        ABOVE_ALL_SESSION_ID=manifest["session_id"],
        ABOVE_ALL_SESSION_DIR=str(session_dir),
    )
    try:
        with (
            (session_dir / "stdout.log").open("wb") as stdout,
            (session_dir / "stderr.log").open("wb") as stderr,
        ):
            child = subprocess.Popen(manifest["command"], stdout=stdout, stderr=stderr, env=env)
            (session_dir / "child.pid").write_text(f"{child.pid}\n")
            code = child.wait()
        status = "done" if code == 0 else "blocked"
        summary = f"{manifest['intent']} - exit {code}"
    except OSError as exc:
        status, summary, code = "blocked", f"command launch failed: {exc}", 127
    _finish(manifest, status, summary)
    return code


def reconcile(scopes: Scopes) -> list[dict]:
    """Mark orphaned pending/running work blocked, preserving its streamed logs."""
    db = migrate(scopes.global_root / "assistant.db", GLOBAL_MIGRATIONS)
    rows = db.execute(
        "SELECT s.id,s.outcome_id,s.source_path FROM sessions s JOIN outcomes o ON o.id=s.outcome_id WHERE o.status IN ('pending','running')"
    ).fetchall()
    db.close()
    reconciled = []
    for session_id, outcome_id, source_path in rows:
        root = Path(source_path)
        manifest_path = root / "worker.json"
        if not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text())
        pid_path = root / "worker.pid"
        if not pid_path.exists():
            # Acceptance is committed before Popen and its pid-file write. Do not
            # race a healthy launcher in that short window; a later reconcile will
            # still catch a launcher that died before recording the pid.
            started = datetime.fromisoformat(manifest["started_at"])
            if (datetime.now(timezone.utc) - started).total_seconds() < 30:
                continue
            pid = -1
        else:
            try:
                pid = int(pid_path.read_text())
            except ValueError:
                pid = -1
        alive = pid > 0
        if alive:
            try:
                os.kill(pid, 0)
            except OSError:
                alive = False
        if alive:
            continue
        if pid > 0:
            # The worker is its process-group leader. If it was killed while its
            # command survived, terminate that orphan before reporting blocked.
            try:
                os.killpg(pid, __import__("signal").SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        summary = "orphaned worker found during reconciliation; outcome blocked, logs preserved"
        _finish(manifest, "blocked", summary)
        reconciled.append({"session_id": session_id, "outcome_id": outcome_id, "status": "blocked"})
    return reconciled


if __name__ == "__main__":
    raise SystemExit(run_worker(Path(sys.argv[1])))
