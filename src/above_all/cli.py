from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from .agent_context import generate_agent_context
from .analysis import analyze
from .async_outcomes import launch_async, reconcile
from .config import configured_command, load_agent_config
from .db import GLOBAL_MIGRATIONS, PROJECT_MIGRATIONS, migrate
from .memory_backend import get_backend
from .notes import create_note, index_note, list_candidates, search_notes
from .paths import resolve_scopes
from .privacy import ensure_project_privacy, validate_project_privacy
from .routing import load_routing, route
from .session_wrapper import wrap_interactive
from .skills import discover, load_selected


def init_scopes(project: bool = True, share_approved: bool = False):
    scopes = resolve_scopes()
    migrate(scopes.global_root / "assistant.db", GLOBAL_MIGRATIONS).close()
    scopes.global_root.mkdir(parents=True, exist_ok=True)
    examples = Path(__file__).parents[2] / "config"
    for name in ("routing", "agent"):
        target = scopes.global_root / f"{name}.toml"
        example = examples / f"{name}.example.toml"
        if not target.exists() and example.exists():
            shutil.copy(example, target)
    if project and scopes.project_root:
        ensure_project_privacy(scopes.project_root.parent, share_approved)
        migrate(scopes.project_root / "assistant.db", PROJECT_MIGRATIONS).close()
        (scopes.project_root / "notes").mkdir(parents=True, exist_ok=True)
        (scopes.project_root / "candidates").mkdir(parents=True, exist_ok=True)
    return scopes


def _strip_separator(cmd: list[str]) -> list[str]:
    return cmd[1:] if cmd[:1] == ["--"] else cmd


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="above-all")
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument(
        "--share-approved", action="store_true",
        help="allow approved project notes and generated context to be committed",
    )
    privacy = sub.add_parser("privacy-check")
    privacy.add_argument(
        "--share-approved", action="store_true",
        help="validate the exact approved-note sharing surface",
    )
    sub.add_parser("scope")
    sub.add_parser("sessions")
    sub.add_parser("reconcile")
    r = sub.add_parser("route")
    r.add_argument("level", choices=("mechanical", "routine", "judgment", "high_stakes"))
    r.add_argument("--signal", action="append", default=[])
    d = sub.add_parser("do")
    d.add_argument("intent")
    d.add_argument("--constraint", action="append", default=[])
    d.add_argument("--source", action="append", default=[])
    d.add_argument("--outcome-id")
    d.add_argument("--skill", action="append", default=[])
    d.add_argument("cmd", nargs=argparse.REMAINDER)
    w = sub.add_parser("work")
    w.add_argument("--skill", action="append", default=[])
    w.add_argument("cmd", nargs=argparse.REMAINDER)
    sk = sub.add_parser("skills")
    sk.add_argument("--json", action="store_true")
    analysis = sub.add_parser("analyze")
    analysis.add_argument("--min-completed", type=int, default=3)
    n = sub.add_parser("note")
    ns = n.add_subparsers(dest="note_command", required=True)
    add = ns.add_parser("add")
    add.add_argument("title")
    add.add_argument("body")
    add.add_argument("--type", default="fact")
    add.add_argument("--source", action="append", required=True)
    add.add_argument("--stale-after")
    search = ns.add_parser("search")
    search.add_argument("query")
    ns.add_parser("candidates")
    approve = ns.add_parser("approve")
    approve.add_argument("candidate_id")
    approve.add_argument("--replace")
    discard = ns.add_parser("discard")
    discard.add_argument("candidate_id")
    ns.add_parser("context")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    scopes = resolve_scopes()
    if args.command == "init":
        scopes = init_scopes(share_approved=args.share_approved)
        print(json.dumps({"global": str(scopes.global_root), "project": str(scopes.project_root) if scopes.project_root else None}))
    elif args.command == "privacy-check":
        if not scopes.project_root:
            raise SystemExit("privacy-check requires a Git project")
        print(json.dumps(validate_project_privacy(scopes.project_root.parent, args.share_approved)))
    elif args.command == "scope":
        print(json.dumps({"global": str(scopes.global_root), "project": str(scopes.project_root) if scopes.project_root else None}))
    elif args.command == "route":
        selected = route(load_routing(scopes.global_root / "routing.toml"), args.level, set(args.signal))
        print(json.dumps(selected.__dict__))
    elif args.command == "sessions":
        init_scopes()
        global_db = migrate(scopes.global_root / "assistant.db", GLOBAL_MIGRATIONS)
        rows = global_db.execute("SELECT * FROM sessions ORDER BY started_at DESC LIMIT 20")
        print(json.dumps([dict(r) for r in rows], default=str))
    elif args.command == "skills":
        items = discover(scopes.global_root, scopes.project_root)
        print(json.dumps([{"name": x.name, "description": x.description, "path": str(x.path)} for x in items.values()]))
    elif args.command == "analyze":
        init_scopes()
        db = migrate(scopes.global_root / "assistant.db", GLOBAL_MIGRATIONS)
        try:
            print(json.dumps(analyze(db, scopes.global_root / "analysis", args.min_completed)))
        finally:
            db.close()
    elif args.command == "do":
        init_scopes()
        config = load_agent_config(scopes.global_root)
        cmd = _strip_separator(args.cmd) or configured_command(config, "headless")
        if not cmd:
            raise SystemExit("no headless command: pass `-- <cmd...>` or configure agent.toml")
        result = launch_async(scopes, cmd, args.intent, provider=config.get("agent_cli", {}).get("provider", "unknown"), outcome_id=args.outcome_id)
        print(json.dumps({"session_id": result.session_id, "outcome_id": result.outcome_id, "status": result.status, "pid": result.pid, "session_dir": str(result.session_dir)}))
    elif args.command == "reconcile":
        init_scopes()
        print(json.dumps(reconcile(scopes)))
    elif args.command == "work":
        init_scopes()
        config = load_agent_config(scopes.global_root)
        backend = get_backend(config.get("memory", {}).get("backend"))
        cmd = _strip_separator(args.cmd) or configured_command(config, "interactive")
        if not cmd:
            raise SystemExit("no interactive command: pass `-- <cmd...>` or configure agent.toml")
        selected = load_selected(scopes.global_root, scopes.project_root, args.skill)
        skill_text = "\n\n".join(f"# {x.name}\n\n{x.body}" for x in selected)
        result = wrap_interactive(
            scopes,
            cmd,
            backend,
            provider=config.get("agent_cli", {}).get("provider", "unknown"),
            skill_text=skill_text,
        )
        print(json.dumps({"session_id": result.session_id, "status": result.status,
                          "exit_code": result.exit_code, "summary": result.summary,
                          "warnings": result.warnings, "session_dir": str(result.session_dir)}))
    elif args.note_command == "add":
        if not scopes.project_root:
            raise SystemExit("note commands require a Git project")
        init_scopes()
        path = create_note(scopes.project_root / "notes", args.title, args.body, args.type, args.source, args.stale_after)
        db = migrate(scopes.project_root / "assistant.db", PROJECT_MIGRATIONS)
        index_note(db, path)
        print(path)
    elif args.note_command == "search":
        if not scopes.project_root:
            raise SystemExit("note commands require a Git project")
        db = migrate(scopes.project_root / "assistant.db", PROJECT_MIGRATIONS)
        print(json.dumps(search_notes(db, args.query)))
    elif args.note_command in {"candidates", "approve", "discard", "context"}:
        if not scopes.project_root:
            raise SystemExit("note commands require a Git project")
        init_scopes()
        backend = get_backend(load_agent_config(scopes.global_root).get("memory", {}).get("backend"))
        if args.note_command == "candidates":
            print(json.dumps(list_candidates(scopes.project_root / "candidates")))
        elif args.note_command == "approve":
            db = migrate(scopes.project_root / "assistant.db", PROJECT_MIGRATIONS)
            print(backend.approve(db, scopes.project_root, args.candidate_id, replaces=args.replace))
        elif args.note_command == "discard":
            print(backend.discard(scopes.project_root, args.candidate_id))
        else:
            db = migrate(scopes.project_root / "assistant.db", PROJECT_MIGRATIONS)
            print(generate_agent_context(scopes.project_root, db, backend))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


