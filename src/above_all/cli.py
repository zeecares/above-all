from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from .db import GLOBAL_MIGRATIONS, PROJECT_MIGRATIONS, migrate
from .notes import create_note, index_note, search_notes
from .paths import resolve_scopes
from .routing import load_routing, route


def init_scopes(project: bool = True):
    scopes = resolve_scopes()
    migrate(scopes.global_root / "assistant.db", GLOBAL_MIGRATIONS).close()
    scopes.global_root.mkdir(parents=True, exist_ok=True)
    routing = scopes.global_root / "routing.toml"
    if not routing.exists():
        example = Path(__file__).parents[2] / "config" / "routing.example.toml"
        if example.exists():
            shutil.copy(example, routing)
    if project and scopes.project_root:
        migrate(scopes.project_root / "assistant.db", PROJECT_MIGRATIONS).close()
        (scopes.project_root / "notes").mkdir(parents=True, exist_ok=True)
    return scopes


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="above-all")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("scope")
    r = sub.add_parser("route")
    r.add_argument("level", choices=("mechanical", "routine", "judgment", "high_stakes"))
    r.add_argument("--signal", action="append", default=[])
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
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    scopes = resolve_scopes()
    if args.command == "init":
        scopes = init_scopes()
        print(json.dumps({"global": str(scopes.global_root), "project": str(scopes.project_root) if scopes.project_root else None}))
    elif args.command == "scope":
        print(json.dumps({"global": str(scopes.global_root), "project": str(scopes.project_root) if scopes.project_root else None}))
    elif args.command == "route":
        selected = route(load_routing(scopes.global_root / "routing.toml"), args.level, set(args.signal))
        print(json.dumps(selected.__dict__))
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
