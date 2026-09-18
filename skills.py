"""Small, explicit SKILL.md extension loader.

Skills are instructions, not automatic authority. A project skill shadows a global
skill with the same name. The CLI only injects skills named with ``--skill``;
a model may suggest one later, but keyword matching is deliberately not routing.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path
    body: str


def _read(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"skill missing YAML frontmatter: {path}")
    try:
        raw, body = text[4:].split("\n---\n", 1)
    except ValueError as exc:
        raise ValueError(f"skill frontmatter is not closed: {path}") from exc
    meta = yaml.safe_load(raw) or {}
    name, description = meta.get("name"), meta.get("description")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"skill missing name: {path}")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"skill missing description/when-to-use trigger: {path}")
    if path.parent.name != name:
        raise ValueError(f"skill directory {path.parent.name!r} must match name {name!r}")
    return Skill(name, description, path, body.strip())


def discover(global_root: Path, project_root: Path | None) -> dict[str, Skill]:
    found: dict[str, Skill] = {}
    roots = [global_root / "skills"]
    if project_root:
        roots.append(project_root / "skills")
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.glob("*/SKILL.md")):
            skill = _read(path)
            found[skill.name] = skill  # project root is visited last and shadows global
    return found


def load_selected(global_root: Path, project_root: Path | None, names: list[str]) -> list[Skill]:
    available = discover(global_root, project_root)
    missing = [name for name in names if name not in available]
    if missing:
        raise ValueError(f"unknown skill(s): {', '.join(missing)}")
    return [available[name] for name in names]
