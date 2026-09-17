from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

APP_DIR = ".above-all"


@dataclass(frozen=True)
class Scopes:
    global_root: Path
    project_root: Path | None


def global_root() -> Path:
    override = os.getenv("ABOVE_ALL_HOME")
    return Path(override).expanduser() if override else Path.home() / APP_DIR


def find_project_root(start: Path | None = None) -> Path | None:
    start = (start or Path.cwd()).resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return Path(result.stdout.strip())


def resolve_scopes(start: Path | None = None) -> Scopes:
    project = find_project_root(start)
    return Scopes(global_root(), project / APP_DIR if project else None)
