"""Bounded weekly-analysis scaffold. It proposes queues; it changes nothing."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .traces import ensure_trace_schema


def analyze(db: sqlite3.Connection, output_root: Path, min_completed: int = 3) -> dict:
    ensure_trace_schema(db)
    completed = db.execute("SELECT COUNT(*) FROM outcomes WHERE status='done'").fetchone()[0]
    queues = {"system": [], "user": [], "eval": []}
    if completed < min_completed:
        return {"status": "quiet", "completed_outcomes": completed, "queues": queues}
    failures = db.execute("""
        SELECT tool_name, COUNT(*) AS failures, GROUP_CONCAT(DISTINCT session_id) AS sessions
        FROM trace_tool_calls WHERE status='failed' GROUP BY tool_name HAVING COUNT(*) >= 2
        ORDER BY failures DESC, tool_name
    """).fetchall()
    for tool, count, sessions in failures:
        queues["system"].append({"kind": "repeated_tool_failure", "tool": tool, "count": count, "evidence_sessions": sessions.split(","), "estimated_value": "reduce repeated blocked work", "proposed_experiment": f"reproduce and harden {tool}"})
    output_root.mkdir(parents=True, exist_ok=True)
    for name, items in queues.items():
        (output_root / f"{name}-candidates.json").write_text(json.dumps(items, indent=2) + "\n", encoding="utf-8")
    return {"status": "proposed" if any(queues.values()) else "quiet", "completed_outcomes": completed, "queues": queues}
