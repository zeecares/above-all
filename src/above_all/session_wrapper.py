"""Session wrapper: launch and harvest disposable agent sessions.

The wrapper is the only component that launches agent-CLI processes. Every
session - headless or interactive - leaves durable state behind when it dies:
a session row in both scopes, an exit summary, and memory candidates. Exit
harvest never writes active memory directly; it creates candidates for review.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .agent_context import merged_active_notes
from .db import GLOBAL_MIGRATIONS, PROJECT_MIGRATIONS, migrate
from .extraction import extract_trace_candidate
from .paths import Scopes
from .traces import find_claude_transcript, import_claude_code_jsonl

RETURN_SHAPE = ["outcome", "evidence", "changes", "blockers"]
MAX_ENVELOPE_NOTES = 5
MAX_ENVELOPE_CHARS = 1500


@dataclass(frozen=True)
class SessionResult:
    session_id: str
    mode: str
    status: str
    exit_code: int
    session_dir: Path
    summary: str
    warnings: list[str] = field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _select_relevant_notes(
    global_db: sqlite3.Connection, project_db: sqlite3.Connection | None, backend
) -> list[dict]:
    notes = merged_active_notes(global_db, project_db, backend, MAX_ENVELOPE_NOTES)
    selected, used = [], 0
    for note in notes:
        text = f"# {note['title']}\n\n_Source: {note['scope']} knowledge_\n\n{note['body'].strip()}"
        if used + len(text) > MAX_ENVELOPE_CHARS:
            continue
        selected.append(
            {"id": note["id"], "title": note["title"], "scope": note["scope"], "text": text}
        )
        used += len(text)
    return selected


def _write_handoff(
    session_dir: Path,
    intent: str,
    constraints: list[str],
    source_anchors: list[str],
    relevant_notes: list[dict],
    outcome_id: str,
) -> dict:
    session_dir.mkdir(parents=True, exist_ok=True)
    prompt_parts = [f"# Intent\n\n{intent}"]
    if constraints:
        prompt_parts.append("# Constraints\n\n" + "\n".join(f"- {c}" for c in constraints))
    if source_anchors:
        prompt_parts.append("# Source anchors\n\n" + "\n".join(f"- {a}" for a in source_anchors))
    for note in relevant_notes:
        prompt_parts.append(note["text"])
    prompt = "\n\n".join(prompt_parts) + "\n"
    (session_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    envelope = {
        "outcome_id": outcome_id,
        "intent": intent,
        "constraints": constraints,
        "source_anchors": source_anchors,
        "relevant_notes": [
            {"id": n["id"], "title": n["title"], "scope": n["scope"]} for n in relevant_notes
        ],
        "return": RETURN_SHAPE,
    }
    (session_dir / "envelope.json").write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    return envelope


def _record_session(db: sqlite3.Connection, session: dict, project: str | None = None) -> None:
    columns = (
        "id,provider,mode,model,outcome_id,parent_session_id,started_at,ended_at,"
        "source_path,import_version,summary,tokens_in,tokens_out,cost_usd"
    )
    values = (
        session["id"],
        session["provider"],
        session["mode"],
        session.get("model"),
        session.get("outcome_id"),
        session.get("parent_session_id"),
        session["started_at"],
        session.get("ended_at"),
        session.get("source_path"),
        session.get("import_version"),
        session.get("summary"),
        session.get("tokens_in"),
        session.get("tokens_out"),
        session.get("cost_usd"),
    )
    with db:
        if project is not None:
            db.execute(
                f"INSERT OR REPLACE INTO sessions (id,provider,project,{columns.split(',', 2)[2]}) "
                f"VALUES (?,?,{','.join('?' * 13)})",
                (session["id"], session["provider"], project, *values[2:]),
            )
        else:
            db.execute(
                f"INSERT OR REPLACE INTO sessions ({columns}) VALUES ({','.join('?' * 14)})",
                values,
            )


def _record_event(db: sqlite3.Connection, kind: str, outcome_id: str | None, payload: dict) -> None:
    with db:
        db.execute(
            "INSERT INTO events (ts,kind,outcome_id,payload_json) VALUES (?,?,?,?)",
            (_now(), kind, outcome_id, json.dumps(payload)),
        )


def _git_diff(project_root: Path | None) -> tuple[str, list[str]]:
    if project_root is None:
        return "", []
    try:
        diff = subprocess.run(
            ["git", "-C", str(project_root), "diff"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        changed = subprocess.run(
            ["git", "-C", str(project_root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return "", []
    return diff, changed


def _open_scope_dbs(scopes: Scopes) -> tuple[sqlite3.Connection, sqlite3.Connection | None]:
    global_db = migrate(scopes.global_root / "assistant.db", GLOBAL_MIGRATIONS)
    project_db = (
        migrate(scopes.project_root / "assistant.db", PROJECT_MIGRATIONS)
        if scopes.project_root
        else None
    )
    return global_db, project_db


def _project_name(scopes: Scopes) -> str | None:
    return scopes.project_root.parent.name if scopes.project_root else None


def _exit_harvest(
    scopes: Scopes,
    session: dict,
    summary: str,
    backend,
    note_extra: dict | None = None,
    trace_path: Path | None = None,
) -> list[str]:
    """Exit harvest, in order: session state -> summary -> candidates -> traces.

    The first two steps must succeed even if candidate creation fails; a failed
    candidate write is a loud warning (event row plus returned warning), never
    a silent drop.
    """
    warnings: list[str] = []
    global_db, project_db = _open_scope_dbs(scopes)
    project = _project_name(scopes)
    try:
        session = dict(session, summary=summary, ended_at=_now())
        _record_session(global_db, session, project=project)
        if project_db is not None:
            _record_session(project_db, session)
            _record_event(project_db, "session_exited", session.get("outcome_id"), session)
        _record_event(global_db, "session_exited", session.get("outcome_id"), session)
        (Path(session["source_path"]) / "summary.md").write_text(summary + "\n", encoding="utf-8")
        scope_dir = scopes.project_root or scopes.global_root
        trace_path = trace_path or find_claude_transcript(Path(session["source_path"]))
        extraction = None
        if trace_path and session.get("provider") == "claude_code":
            try:
                result = import_claude_code_jsonl(global_db, trace_path, session["id"])
                extraction = extract_trace_candidate(trace_path, session["id"])
                usage = global_db.execute(
                    "SELECT tokens_in,tokens_out,reported_cost_usd FROM trace_usage WHERE session_id=?",
                    (session["id"],),
                ).fetchone()
                if usage is not None:
                    with global_db:
                        global_db.execute(
                            "UPDATE sessions SET tokens_in=?,tokens_out=?,cost_usd=? WHERE id=?",
                            (*usage, session["id"]),
                        )
                    if project_db is not None:
                        with project_db:
                            project_db.execute(
                                "UPDATE sessions SET tokens_in=?,tokens_out=?,cost_usd=? WHERE id=?",
                                (*usage, session["id"]),
                            )
                _record_event(global_db, "trace_imported", session.get("outcome_id"), result)
            except (OSError, ValueError, sqlite3.Error) as exc:
                warning = f"trace import/extraction failed for session {session['id']}: {exc}"
                _record_event(global_db, "warning", session.get("outcome_id"), {"warning": warning})
                print(f"WARNING: {warning}", file=sys.stderr)
                warnings.append(warning)
        else:
            _record_event(
                global_db,
                "trace_import_skipped",
                session.get("outcome_id"),
                {"session_id": session["id"], "reason": "no explicit stock Claude Code transcript"},
            )

        if extraction and extraction.body:
            try:
                candidate_id = f"session-{session['id']}"
                candidate_path = scope_dir / "candidates" / f"{candidate_id}.md"
                if not candidate_path.exists():
                    backend.create_candidate(
                        scope_dir,
                        title=f"Learned from session {session['id']}",
                        body=extraction.body,
                        note_type="fact",
                        sources=[f"session:{session['id']}", *extraction.evidence]
                        + (
                            [f"outcome:{session['outcome_id']}"]
                            if session.get("outcome_id")
                            else []
                        ),
                        extra={**(note_extra or {}), "extraction_method": extraction.method},
                        candidate_id=candidate_id,
                    )
                    _record_event(
                        global_db,
                        "candidate_created",
                        session.get("outcome_id"),
                        {"session_id": session["id"], "method": extraction.method},
                    )
            except (OSError, ValueError, sqlite3.Error) as exc:
                warning = f"candidate creation failed for session {session['id']}: {exc}"
                _record_event(global_db, "warning", session.get("outcome_id"), {"warning": warning})
                print(f"WARNING: {warning}", file=sys.stderr)
                warnings.append(warning)
        else:
            reason = extraction.reason if extraction else "no explicit supported transcript"
            _record_event(
                global_db,
                "candidate_suppressed",
                session.get("outcome_id"),
                {"session_id": session["id"], "reason": reason},
            )
    finally:
        global_db.close()
        if project_db is not None:
            project_db.close()
    return warnings


def _session_env(session_id: str, session_dir: Path) -> dict:
    env = dict(os.environ)
    env.update(
        {
            "ABOVE_ALL_SESSION_ID": session_id,
            "ABOVE_ALL_SESSION_DIR": str(session_dir),
            "ABOVE_ALL_PROMPT": str(session_dir / "prompt.md"),
            "ABOVE_ALL_ENVELOPE": str(session_dir / "envelope.json"),
        }
    )
    return env


def dispatch_headless(
    scopes: Scopes,
    command: list[str],
    intent: str,
    backend,
    constraints: list[str] | None = None,
    source_anchors: list[str] | None = None,
    outcome_id: str | None = None,
    provider: str = "unknown",
) -> SessionResult:
    """Run an agent CLI headlessly with a handoff envelope; harvest on exit."""
    session_id = uuid4().hex[:12]
    outcome_id = outcome_id or f"out-{uuid4().hex[:8]}"
    scope_dir = scopes.project_root or scopes.global_root
    session_dir = scope_dir / "sessions" / session_id
    global_db, project_db = _open_scope_dbs(scopes)
    try:
        relevant = _select_relevant_notes(global_db, project_db, backend)
        envelope = _write_handoff(
            session_dir,
            intent,
            constraints or [],
            source_anchors or [],
            relevant,
            outcome_id,
        )
        session = {
            "id": session_id,
            "provider": provider,
            "mode": "headless",
            "outcome_id": outcome_id,
            "started_at": _now(),
            "source_path": str(session_dir),
        }
        _record_session(global_db, session, project=_project_name(scopes))
        if project_db is not None:
            _record_session(project_db, session)
            _record_event(
                project_db,
                "outcome_accepted",
                outcome_id,
                {"intent": intent, "session_id": session_id},
            )
        _record_event(global_db, "session_started", outcome_id, session)
    finally:
        global_db.close()
        if project_db is not None:
            project_db.close()

    started = time.monotonic()
    run = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env=_session_env(session_id, session_dir),
        check=False,
    )
    duration = time.monotonic() - started
    (session_dir / "stdout.log").write_text(run.stdout, encoding="utf-8")
    (session_dir / "stderr.log").write_text(run.stderr, encoding="utf-8")
    diff, changed = _git_diff(scopes.project_root.parent if scopes.project_root else None)
    (session_dir / "diff.patch").write_text(diff, encoding="utf-8")
    status = "done" if run.returncode == 0 else "failed"
    summary = (
        f"{intent} - exit {run.returncode} after {duration:.1f}s; "
        f"{len(changed)} path(s) changed in the worktree."
    )
    (session_dir / "result.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "outcome_id": envelope["outcome_id"],
                "status": status,
                "exit_code": run.returncode,
                "duration_s": round(duration, 3),
                "changed_paths": changed,
                "diff_file": "diff.patch",
                "return": RETURN_SHAPE,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    global_db, project_db = _open_scope_dbs(scopes)
    try:
        outcome_status = "done" if status == "done" else "blocked"
        for db, project in ((global_db, _project_name(scopes)), (project_db, None)):
            if db is None:
                continue
            with db:
                if project is not None:
                    db.execute(
                        "INSERT OR REPLACE INTO outcomes (id,project,title,status,owner,source_anchor,created_at,updated_at) "
                        "VALUES (?,?,?,?,?,?,COALESCE((SELECT created_at FROM outcomes WHERE id=?),?),?)",
                        (
                            outcome_id,
                            project,
                            intent,
                            outcome_status,
                            f"session:{session_id}",
                            f"session:{session_id}",
                            outcome_id,
                            _now(),
                            _now(),
                        ),
                    )
                else:
                    db.execute(
                        "INSERT OR REPLACE INTO outcomes (id,title,status,owner,source_anchor,created_at,updated_at) "
                        "VALUES (?,?,?,?,?,COALESCE((SELECT created_at FROM outcomes WHERE id=?),?),?)",
                        (
                            outcome_id,
                            intent,
                            outcome_status,
                            f"session:{session_id}",
                            f"session:{session_id}",
                            outcome_id,
                            _now(),
                            _now(),
                        ),
                    )
    finally:
        global_db.close()
        if project_db is not None:
            project_db.close()

    warnings = _exit_harvest(
        scopes,
        session,
        summary,
        backend,
        note_extra={"observer": "above-all", "subject": _project_name(scopes) or "global"},
    )
    return SessionResult(
        session_id, "headless", status, run.returncode, session_dir, summary, warnings
    )


def wrap_interactive(
    scopes: Scopes,
    command: list[str],
    backend,
    provider: str = "unknown",
    skill_text: str = "",
) -> SessionResult:
    """Wrap a live agent-CLI session: preload project knowledge, harvest on exit."""
    from .agent_context import generate_agent_context

    session_id = uuid4().hex[:12]
    scope_dir = scopes.project_root or scopes.global_root
    session_dir = scope_dir / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    context_path = None
    skills_path = None
    if skill_text:
        skills_path = session_dir / "SELECTED_SKILLS.md"
        skills_path.write_text(skill_text.rstrip() + "\n", encoding="utf-8")
    global_db, project_db = _open_scope_dbs(scopes)
    try:
        if project_db is not None:
            context_path = generate_agent_context(
                scopes.project_root, project_db, backend, global_db
            )
        session = {
            "id": session_id,
            "provider": provider,
            "mode": "interactive",
            "started_at": _now(),
            "source_path": str(session_dir),
        }
        _record_session(global_db, session, project=_project_name(scopes))
        if project_db is not None:
            _record_session(project_db, session)
        _record_event(global_db, "session_started", None, session)
    finally:
        global_db.close()
        if project_db is not None:
            project_db.close()

    started = time.monotonic()
    env = _session_env(session_id, session_dir)
    if skills_path:
        env["ABOVE_ALL_SKILLS"] = str(skills_path)
    exit_code = subprocess.call(command, env=env)
    duration = time.monotonic() - started
    status = "done" if exit_code == 0 else "failed"
    context_note = f"; context preloaded from {context_path}" if context_path else ""
    summary = f"interactive session - exit {exit_code} after {duration:.1f}s{context_note}."
    warnings = _exit_harvest(
        scopes,
        session,
        summary,
        backend,
        note_extra={"observer": "above-all", "subject": _project_name(scopes) or "global"},
    )
    return SessionResult(
        session_id, "interactive", status, exit_code, session_dir, summary, warnings
    )
