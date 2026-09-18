from __future__ import annotations

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib
from dataclasses import dataclass
from pathlib import Path

LEVELS = ("mechanical", "routine", "judgment", "high_stakes")


@dataclass(frozen=True)
class Route:
    tier: str
    endpoint: str
    model: str


def load_routing(path: Path) -> dict:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    for level in LEVELS:
        tier = data["levels"][level]["tier"]
        if tier not in data["models"]:
            raise ValueError(f"unknown tier {tier!r} for {level}")
    return data


def route(data: dict, level: str, signals: set[str] | None = None) -> Route:
    if level not in LEVELS:
        raise ValueError(f"unknown judgment level: {level}")
    tier = data["levels"][level]["tier"]
    signals = signals or set()
    for rule in data.get("escalation", []):
        if tier == rule["from"] and signals.intersection(rule["when"]):
            tier = rule["to"]
            break
    model = data["models"][tier]
    return Route(tier, model["endpoint"], model["model"])



def classify_value(value: str, payload: dict) -> dict | None:
    """Placeholder routing seam for a future model-backed value classifier.

    The control plane fails closed until a configured provider implements this call.
    """
    return None
