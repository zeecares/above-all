"""Commit-safety policy for project-local assistant state."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .notes import parse_note

APP_DIR = ".above-all"
_POLICY = ".gitignore"
_CONTEXT = "AGENT_CONTEXT.md"
_NOTE_ID = re.compile(r"[0-9a-f]{32}\.md")


def _approved_note_names(project_root: Path) -> tuple[str, ...]:
    notes = project_root / APP_DIR / "notes"
    if not notes.is_dir():
        return ()
    approved = []
    for path in sorted(notes.iterdir()):
        if not path.is_file() or not _NOTE_ID.fullmatch(path.name):
            continue
        try:
            note = parse_note(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        if note.metadata["status"] == "active":
            approved.append(path.name)
    return tuple(approved)


def _allowed_paths(project_root: Path, share_approved: bool) -> set[str]:
    allowed = {f"{APP_DIR}/{_POLICY}"}
    if share_approved:
        allowed.add(f"{APP_DIR}/{_CONTEXT}")
        allowed.update(f"{APP_DIR}/notes/{name}" for name in _approved_note_names(project_root))
    return allowed


def ignore_policy(approved_note_names: tuple[str, ...] = (), share_approved: bool = False) -> str:
    """Return a fail-closed policy with exact exceptions for reviewed knowledge."""
    lines = ["*", "!.gitignore"]
    if share_approved:
        lines.append("!notes/")
        lines.extend(f"!notes/{name}" for name in approved_note_names)
        lines.append("!AGENT_CONTEXT.md")
    return "\n".join(lines) + "\n"


def _tracked_paths(project_root: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "ls-files", APP_DIR],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("cannot verify project privacy policy with git") from exc
    return [path for path in result.stdout.splitlines() if path]


def validate_project_privacy(project_root: Path, share_approved: bool = False) -> dict:
    """Validate tracked state and the installed policy against the selected mode."""
    approved = _approved_note_names(project_root) if share_approved else ()
    allowed = _allowed_paths(project_root, share_approved)
    unsafe = sorted(set(_tracked_paths(project_root)) - allowed)
    if unsafe:
        raise RuntimeError(f"refusing unsafe tracked above-all state: {', '.join(unsafe)}")

    policy_path = project_root / APP_DIR / _POLICY
    expected = ignore_policy(approved, share_approved)
    if not policy_path.is_file() or policy_path.read_text(encoding="utf-8") != expected:
        raise RuntimeError("installed above-all privacy policy does not match selected sharing mode")
    return {
        "status": "safe",
        "mode": "share-approved" if share_approved else "local-only",
        "shared_notes": list(approved),
    }


def ensure_project_privacy(project_root: Path, share_approved: bool = False) -> Path:
    """Install the selected policy, refusing tracked state outside its exact public surface."""
    approved = _approved_note_names(project_root) if share_approved else ()
    allowed = _allowed_paths(project_root, share_approved)
    unsafe = sorted(set(_tracked_paths(project_root)) - allowed)
    if unsafe:
        raise RuntimeError(f"refusing unsafe tracked above-all state: {', '.join(unsafe)}")

    path = project_root / APP_DIR / _POLICY
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ignore_policy(approved, share_approved), encoding="utf-8")
    validate_project_privacy(project_root, share_approved)
    return path
