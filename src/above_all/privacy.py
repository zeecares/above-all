"""Commit-safety policy for project-local assistant state."""

from __future__ import annotations

import subprocess
from pathlib import Path

APP_DIR = ".above-all"
PRIVATE_NAMES = {
    "assistant.db",
    "candidates",
    "sessions",
    "analysis",
    "traces",
    "logs",
    "SELECTED_SKILLS.md",
}


def ignore_policy(share_approved: bool = False) -> str:
    """Return a safe project policy, optionally allowing reviewed knowledge."""
    lines = ["*"]
    if share_approved:
        lines += [
            "!notes/",
            "!notes/**",
            "!AGENT_CONTEXT.md",
        ]
    return "\n".join(lines) + "\n"


def _tracked_private_paths(project_root: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "ls-files", APP_DIR],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("cannot verify project privacy policy with git") from exc
    unsafe = []
    for path in result.stdout.splitlines():
        relative = Path(path).relative_to(APP_DIR)
        if relative.parts and relative.parts[0] in PRIVATE_NAMES:
            unsafe.append(path)
    return unsafe


def ensure_project_privacy(project_root: Path, share_approved: bool = False) -> Path:
    """Install the selected ignore policy, refusing already-tracked private state."""
    tracked = _tracked_private_paths(project_root)
    if tracked:
        joined = ", ".join(tracked)
        raise RuntimeError(f"refusing unsafe tracked above-all state: {joined}")

    path = project_root / APP_DIR / ".gitignore"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ignore_policy(share_approved), encoding="utf-8")
    return path
