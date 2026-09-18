from __future__ import annotations

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib
from pathlib import Path


def load_agent_config(global_root: Path) -> dict:
    path = global_root / "agent.toml"
    if not path.is_file():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8"))


def configured_command(config: dict, mode: str) -> list[str] | None:
    key = "headless_command" if mode == "headless" else "interactive_command"
    command = config.get("agent_cli", {}).get(key)
    return list(command) if command else None
