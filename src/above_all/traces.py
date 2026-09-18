"""Trace-plane adapter for the stock public Claude Code JSONL format."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TRACE_SCHEMA = """
CREATE TABLE IF NOT EXISTS trace_messages (
  source_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, seq INTEGER NOT NULL,
  role TEXT NOT NULL, ts TEXT, text TEXT, active_branch INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS trace_tool_calls (
  source_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, seq INTEGER NOT NULL,
  tool_name TEXT NOT NULL, ts TEXT, status TEXT, error_class TEXT, duration_ms INTEGER
);
CREATE TABLE IF NOT EXISTS trace_usage (
  session_id TEXT PRIMARY KEY, tokens_in INTEGER, tokens_out INTEGER,
  cached_tokens INTEGER, reported_cost_usd REAL, source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trace_imports (
  source_path TEXT PRIMARY KEY, provider TEXT NOT NULL, format_version TEXT,
  fingerprint TEXT NOT NULL, imported_at TEXT NOT NULL, status TEXT NOT NULL, warning TEXT
);
"""


@dataclass
class ParsedTrace:
    session_id: str
    messages: list[tuple] = field(default_factory=list)
    tools: list[tuple] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cached_tokens: int = 0
    warnings: list[str] = field(default_factory=list)


def ensure_trace_schema(db: sqlite3.Connection) -> None:
    db.executescript(TRACE_SCHEMA)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block.get("text", "") for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def parse_claude_code_jsonl(path: Path, fallback_session_id: str | None = None) -> ParsedTrace:
    parsed: ParsedTrace | None = None
    tool_results: dict[str, tuple[str, str | None]] = {}
    seen_ids: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON at line {line_no}: {exc.msg}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"record at line {line_no} is not an object")
            session_id = row.get("sessionId") or fallback_session_id
            if not session_id:
                raise ValueError(f"record at line {line_no} has no sessionId")
            if parsed is None:
                parsed = ParsedTrace(session_id)
            elif parsed.session_id != session_id:
                raise ValueError(f"multiple sessionId values in {path}")
            kind = row.get("type")
            uuid = str(row.get("uuid") or f"line:{line_no}")
            if uuid in seen_ids:
                raise ValueError(f"duplicate source id {uuid!r}")
            seen_ids.add(uuid)
            active = 0 if row.get("isSidechain") else 1
            ts = row.get("timestamp")
            message = row.get("message") if isinstance(row.get("message"), dict) else {}
            if kind in {"user", "assistant"}:
                content = message.get("content", row.get("content", ""))
                parsed.messages.append((uuid, parsed.session_id, line_no, kind, ts, _text(content), active))
                if kind == "assistant" and active:
                    usage = message.get("usage") or row.get("usage") or {}
                    parsed.tokens_in += int(usage.get("input_tokens") or 0)
                    parsed.tokens_out += int(usage.get("output_tokens") or 0)
                    parsed.cached_tokens += int(usage.get("cache_read_input_tokens") or 0)
                    for index, block in enumerate(content if isinstance(content, list) else []):
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            call_id = str(block.get("id") or f"{uuid}:tool:{index}")
                            parsed.tools.append((call_id, parsed.session_id, line_no, str(block.get("name") or "unknown"), ts, "unknown", None, None))
                elif kind == "user" and active:
                    for block in content if isinstance(content, list) else []:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            call_id = block.get("tool_use_id")
                            if call_id:
                                failed = bool(block.get("is_error"))
                                body = _text(block.get("content"))
                                tool_results[str(call_id)] = ("failed" if failed else "ok", body[:200] if failed else None)
            elif kind in {"system", "summary", "progress", "queue-operation", "file-history-snapshot"}:
                # Markers are known stock records but outside the four normalized outputs.
                continue
            else:
                parsed.warnings.append(f"line {line_no}: unsupported record type {kind!r}")
    if parsed is None:
        raise ValueError(f"empty transcript: {path}")
    parsed.tools = [
        (*tool[:5], *tool_results.get(tool[0], (tool[5], tool[6])), tool[7])
        for tool in parsed.tools
    ]
    return parsed


def import_claude_code_jsonl(db: sqlite3.Connection, path: Path, fallback_session_id: str | None = None) -> dict:
    ensure_trace_schema(db)
    data = path.read_bytes()
    fingerprint = hashlib.sha256(data).hexdigest()
    source_path = str(path.resolve())
    existing = db.execute("SELECT fingerprint,status,warning FROM trace_imports WHERE source_path=?", (source_path,)).fetchone()
    if existing:
        existing_fingerprint, existing_status, existing_warning = existing
        if existing_fingerprint == fingerprint:
            if existing_status == "imported":
                return {"status": "noop", "fingerprint": fingerprint, "warnings": []}
            raise ValueError(existing_warning or f"previous trace import status is {existing_status}")
        warning = "source fingerprint changed; refusing silent overwrite"
        with db:
            db.execute("UPDATE trace_imports SET status='changed',warning=? WHERE source_path=?", (warning, source_path))
        raise ValueError(warning)
    try:
        trace = parse_claude_code_jsonl(path, fallback_session_id)
        with db:
            db.executemany("INSERT INTO trace_messages VALUES (?,?,?,?,?,?,?)", trace.messages)
            db.executemany("INSERT INTO trace_tool_calls VALUES (?,?,?,?,?,?,?,?)", trace.tools)
            db.execute("INSERT INTO trace_usage VALUES (?,?,?,?,?,?)", (trace.session_id, trace.tokens_in, trace.tokens_out, trace.cached_tokens, None, "claude_code_jsonl"))
            db.execute("INSERT INTO trace_imports VALUES (?,?,?,?,?,?,?)", (source_path, "claude_code", "public-jsonl-v1", fingerprint, datetime.now(timezone.utc).isoformat(), "imported", "\n".join(trace.warnings) or None))
    except (ValueError, sqlite3.Error) as exc:
        with db:
            db.execute("INSERT OR REPLACE INTO trace_imports VALUES (?,?,?,?,?,?,?)", (source_path, "claude_code", "public-jsonl-v1", fingerprint, datetime.now(timezone.utc).isoformat(), "failed", str(exc)))
        raise
    return {"status": "imported", "session_id": trace.session_id, "messages": len(trace.messages), "tool_calls": len(trace.tools), "warnings": trace.warnings}


def find_claude_transcript(session_dir: Path) -> Path | None:
    """Use only explicit wrapper-owned locations; never guess among concurrent sessions."""
    for name in ("transcript.jsonl", "claude-code.jsonl"):
        candidate = session_dir / name
        if candidate.is_file():
            return candidate
    return None
