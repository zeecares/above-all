"""First-run diagnostics (`doctor`) and the operational dashboard (`status`).

Both are strictly read-only: they never create or migrate stores, never write
config, and never print secret material. Every failure names its remediation.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import load_agent_config
from .db import GLOBAL_MIGRATIONS
from .notes import list_candidates
from .paths import Scopes
from .privacy import validate_project_privacy
from .routing import load_routing


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

PLACEHOLDER_PROVIDER = "your-agent-cli"
PLACEHOLDER_ENDPOINT_MARK = ".example"


def _readonly_db(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _check(name: str, status: str, detail: str, fix: str | None = None) -> dict:
    return {"name": name, "status": status, "detail": detail, "fix": fix}


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _config_checks(scopes: Scopes) -> tuple[list[dict], dict | None]:
    checks = []
    agent_path = scopes.global_root / "agent.toml"
    if not agent_path.is_file():
        checks.append(_check(
            "agent config", "fail", f"no agent.toml in {scopes.global_root}",
            f"run `above-all init`, then edit {agent_path} to map your real agent CLI",
        ))
        return checks, None
    try:
        config = load_agent_config(scopes.global_root)
    except (OSError, ValueError) as exc:
        checks.append(_check(
            "agent config", "fail", f"agent.toml is not valid TOML: {exc}",
            f"fix the syntax in {agent_path}",
        ))
        return checks, None
    cli = config.get("agent_cli", {})
    provider = cli.get("provider", "")
    commands = " ".join(
        token
        for key in ("headless_command", "interactive_command")
        for token in (cli.get(key) or [])
    )
    if provider == PLACEHOLDER_PROVIDER or PLACEHOLDER_PROVIDER in commands:
        checks.append(_check(
            "agent config", "fail",
            "agent.toml still holds placeholder provider/commands",
            f"edit {agent_path}: set agent_cli.provider plus headless_command and "
            "interactive_command to your real agent CLI build",
        ))
    elif not cli.get("headless_command") or not cli.get("interactive_command"):
        checks.append(_check(
            "agent config", "fail",
            "agent.toml is missing headless_command or interactive_command",
            f"edit {agent_path}: set both commands so `do` and `work` can launch",
        ))
    else:
        checks.append(_check("agent config", "ok", f"provider {provider!r} with both commands"))
    backend_name = config.get("memory", {}).get("backend")
    if backend_name:
        try:
            from .memory_backend import get_backend

            get_backend(backend_name)
            checks.append(_check("memory backend", "ok", f"backend {backend_name!r}"))
        except ValueError as exc:
            checks.append(_check("memory backend", "fail", str(exc),
                                 f"edit {agent_path}: choose a registered backend"))
    return checks, config


def _routing_checks(scopes: Scopes) -> list[dict]:
    path = scopes.global_root / "routing.toml"
    if not path.is_file():
        return [_check(
            "routing", "fail", f"no routing.toml in {scopes.global_root}",
            f"run `above-all init`, then edit {path} with your real model endpoints",
        )]
    try:
        data = load_routing(path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [_check("routing", "fail", f"routing.toml invalid: {exc}",
                       f"fix {path}: every level must reference a defined tier")]
    invalid_models = [
        tier for tier, model in data.get("models", {}).items()
        if not str(model.get("endpoint", "")).strip()
        or not str(model.get("model", "")).strip()
        or PLACEHOLDER_ENDPOINT_MARK in str(model.get("endpoint", ""))
    ]
    if invalid_models:
        return [_check(
            "routing", "fail",
            f"routing.toml has missing or placeholder endpoint/model values for: "
            f"{', '.join(invalid_models)}",
            f"edit {path}: map each tier to a real model gateway endpoint and model",
        )]
    return [_check("routing", "ok", f"{len(data.get('models', {}))} tiers configured")]


def _transcript_checks(config: dict | None) -> list[dict]:
    provider = (config or {}).get("agent_cli", {}).get("provider")
    if not provider or provider == PLACEHOLDER_PROVIDER:
        return [_check(
            "transcript capture", "fail", "no provider configured",
            "set agent_cli.provider in agent.toml; the stock public Claude Code "
            "JSONL adapter activates for provider 'claude_code'",
        )]
    if provider == "claude_code":
        return [_check(
            "transcript capture", "ok",
            "stock Claude Code JSONL adapter active (wrapper-owned transcript paths only)",
        )]
    return [_check(
        "transcript capture", "warn",
        f"no transcript adapter for provider {provider!r}; the trace plane stays inactive",
        "only the stock public Claude Code JSONL format is supported; internal or "
        "other builds need an adapter confirmed against their real transcript format "
        "before traces can be imported",
    )]


def _store_checks(scopes: Scopes) -> list[dict]:
    checks = []
    db_path = scopes.global_root / "assistant.db"
    db = _readonly_db(db_path)
    if db is None:
        return [_check(
            "global store", "fail", f"no store at {db_path}",
            "run `above-all init` to create and migrate the global store",
        )]
    if not _table_exists(db, "schema_migrations"):
        checks.append(_check(
            "global store", "fail",
            "store predates schema_migrations tracking",
            "run `above-all init`: migrations adopt existing tables and record versions",
        ))
        db.close()
        return checks
    applied = {r[0] for r in db.execute("SELECT version FROM schema_migrations")}
    expected = set(range(1, len(GLOBAL_MIGRATIONS) + 1))
    if applied != expected:
        checks.append(_check(
            "global store", "fail",
            f"schema behind: applied {sorted(applied)}, expected {sorted(expected)}",
            "run `above-all init` to apply pending migrations",
        ))
    else:
        checks.append(_check("global store", "ok", f"schema v{max(applied)} current"))
    missing = []
    if _table_exists(db, "notes"):
        missing = [
            row["id"] for row in db.execute("SELECT id, path FROM notes WHERE status='active'")
            if not Path(row["path"]).is_file()
        ]
    if missing:
        checks.append(_check(
            "note files", "fail",
            f"{len(missing)} indexed active note(s) missing on disk: {', '.join(missing[:5])}",
            "restore the files or reindex; the expiry sweep refuses to run while "
            "indexed notes are missing",
        ))
    db.close()
    return checks


def _privacy_checks(scopes: Scopes) -> list[dict]:
    if not scopes.project_root:
        return [_check("privacy", "ok", "no project scope (global only)")]
    try:
        result = validate_project_privacy(scopes.project_root.parent)
    except RuntimeError as exc:
        return [_check(
            "privacy", "fail", str(exc),
            "untrack the offending state or rerun `above-all init` to reinstall the "
            "fail-closed policy",
        )]
    return [_check("privacy", "ok", f"policy verified ({result['mode']})")]


def _backlog_counts(scopes: Scopes) -> dict:
    counts = {"candidates": 0, "proposed_changesets": 0, "due_watches": 0, "blocked_outcomes": 0}
    scope_dir = scopes.project_root or scopes.global_root
    candidates = scope_dir / "candidates"
    if candidates.is_dir():
        counts["candidates"] = len(list_candidates(candidates))
    changesets = scope_dir / "changesets"
    if changesets.is_dir():
        for path in changesets.glob("changeset-*.json"):
            try:
                if json.loads(path.read_text()).get("status") == "proposed":
                    counts["proposed_changesets"] += 1
            except (OSError, json.JSONDecodeError):
                continue
    db = _readonly_db(scope_dir / "assistant.db")
    if db is not None:
        now = _now_iso()
        if _table_exists(db, "watches"):
            counts["due_watches"] = db.execute(
                "SELECT COUNT(*) FROM watches"
                " WHERE next_fire_at IS NOT NULL AND next_fire_at <= ?",
                (now,),
            ).fetchone()[0]
        if _table_exists(db, "outcomes"):
            counts["blocked_outcomes"] = db.execute(
                "SELECT COUNT(*) FROM outcomes WHERE status='blocked'"
            ).fetchone()[0]
        db.close()
    return counts


def run_doctor(scopes: Scopes) -> dict:
    checks = [_check(
        "scope", "ok",
        f"global {scopes.global_root}"
        + (f", project {scopes.project_root}" if scopes.project_root else ", no project"),
    )]
    config_checks, config = _config_checks(scopes)
    checks.extend(config_checks)
    checks.extend(_routing_checks(scopes))
    checks.extend(_transcript_checks(config))
    checks.extend(_store_checks(scopes))
    checks.extend(_privacy_checks(scopes))
    backlog = _backlog_counts(scopes)
    if backlog["candidates"]:
        checks.append(_check(
            "pending reviews", "warn",
            f"{backlog['candidates']} candidate(s) awaiting review",
            "run `above-all note candidates`, then approve or discard each",
        ))
    if backlog["proposed_changesets"]:
        checks.append(_check(
            "pending reviews", "warn",
            f"{backlog['proposed_changesets']} proposed changeset(s) awaiting decision",
            "review the files under changesets/ and apply with --approve or delete them",
        ))
    if backlog["blocked_outcomes"]:
        checks.append(_check(
            "outcomes", "warn",
            f"{backlog['blocked_outcomes']} blocked outcome(s)",
            "run `above-all status` and inspect the blocked sessions' logs",
        ))
    failed = any(c["status"] == "fail" for c in checks)
    return {"ok": not failed, "checks": checks}


def run_status(scopes: Scopes) -> dict:
    scope_dir = scopes.project_root or scopes.global_root
    db = _readonly_db(scope_dir / "assistant.db")
    report = {"scope": str(scope_dir), "initialized": db is not None}
    if db is None:
        report["hint"] = "run `above-all init` first"
        return report
    if _table_exists(db, "outcomes"):
        report["outcomes"] = dict(
            db.execute("SELECT status, COUNT(*) FROM outcomes GROUP BY status").fetchall()
        )
    else:
        report["outcomes"] = {}
    if _table_exists(db, "sessions"):
        report["sessions"] = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        tokens = db.execute(
            "SELECT COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0) FROM sessions"
        ).fetchone()
        report["tokens_in"], report["tokens_out"] = tokens[0], tokens[1]
    else:
        report["sessions"] = 0
        report["tokens_in"], report["tokens_out"] = 0, 0
    now = _now_iso()
    if _table_exists(db, "watches"):
        report["watches"] = {
            "total": db.execute("SELECT COUNT(*) FROM watches").fetchone()[0],
            "due": db.execute(
                "SELECT COUNT(*) FROM watches"
                " WHERE next_fire_at IS NOT NULL AND next_fire_at <= ?",
                (now,),
            ).fetchone()[0],
            "next_fire_at": db.execute(
                "SELECT MIN(next_fire_at) FROM watches"
                " WHERE next_fire_at IS NOT NULL AND next_fire_at > ?",
                (now,),
            ).fetchone()[0],
        }
    else:
        report["watches"] = {"total": 0, "due": 0, "next_fire_at": None}
    report["warnings"] = (
        db.execute("SELECT COUNT(*) FROM events WHERE kind='warning'").fetchone()[0]
        if _table_exists(db, "events")
        else 0
    )
    db.close()
    report.update(_backlog_counts(scopes))
    return report
