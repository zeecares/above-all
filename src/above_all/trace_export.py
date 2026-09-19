"""Deterministic, scoped export of stock Claude Code JSONL traces."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXPORT_SCHEMA_VERSION = 1
_SECRET_KEYS = re.compile(r"(?i)(?:^|[_-])(?:token|secret|password|passwd|api[_-]?key|credential|authorization|auth)s?(?:$|[_-])")
_SECRET_TEXT = [
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\b(?:sk|gh[opusr]|xox[baprs])[-_][A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)(\\?[\"']?\b(?:token|secret|password|passwd|api[_-]?key)\\?[\"']?\s*[=:]\s*\\?[\"']?)[^\s,;\"'\\]+"),
]


@dataclass(frozen=True)
class ExportSelection:
    session_ids: tuple[str, ...] = ()
    outcome_id: str | None = None
    project: str | None = None
    since: str | None = None
    until: str | None = None

    def validate(self) -> None:
        if not any((self.session_ids, self.outcome_id, self.project, self.since, self.until)):
            raise ValueError("export requires at least one explicit scope filter")


def _secret_env_values() -> tuple[str, ...]:
    values = {
        value for key, value in os.environ.items()
        if _SECRET_KEYS.search(key) and len(value) >= 8
    }
    return tuple(sorted(values, key=lambda value: (-len(value), value)))


def _redact_string(value: str, home: Path, env_values: Iterable[str]) -> tuple[str, int]:
    count = 0
    home_text = str(home.expanduser().resolve())
    if home_text and home_text != "/":
        value, n = re.subn(re.escape(home_text) + r"(?=/|\b)", "~", value)
        count += n
    for secret in env_values:
        if secret in value:
            occurrences = value.count(secret)
            value = value.replace(secret, "[REDACTED_ENV]")
            count += occurrences
    for pattern in _SECRET_TEXT:
        if pattern.groups:
            value, n = pattern.subn(lambda match: match.group(1) + "[REDACTED]", value)
        else:
            value, n = pattern.subn("[REDACTED]", value)
        count += n
    return value, count


def _redact_secret_value(value: Any) -> tuple[Any, int]:
    """Blank string leaves under a secret-looking key at any depth.

    Non-string scalars are kept so numeric fields with secret-adjacent names
    (usage.input_tokens / output_tokens) survive.
    """
    if isinstance(value, str):
        return ("[REDACTED]", 1) if value else (value, 0)
    if isinstance(value, list):
        items, count = [], 0
        for entry in value:
            redacted, found = _redact_secret_value(entry)
            items.append(redacted); count += found
        return items, count
    if isinstance(value, dict):
        result, count = {}, 0
        for key, entry in value.items():
            redacted, found = _redact_secret_value(entry)
            result[key] = redacted; count += found
        return result, count
    return value, 0


def redact(value: Any, home: Path | None = None, env_values: Iterable[str] | None = None) -> tuple[Any, int]:
    """Redact string leaves while retaining the stock JSONL record shape."""
    home = home or Path.home()
    env_values = tuple(env_values) if env_values is not None else _secret_env_values()
    if isinstance(value, str):
        return _redact_string(value, home, env_values)
    if isinstance(value, list):
        result, count = [], 0
        for item in value:
            redacted, found = redact(item, home, env_values)
            result.append(redacted); count += found
        return result, count
    if isinstance(value, dict):
        result, count = {}, 0
        for key, item in value.items():
            if _SECRET_KEYS.search(str(key)):
                result[key], found = _redact_secret_value(item)
            else:
                result[key], found = redact(item, home, env_values)
            count += found
        return result, count
    return value, 0


def _selected(row: sqlite3.Row, selection: ExportSelection, project_name: str | None) -> bool:
    if selection.session_ids and row["id"] not in selection.session_ids:
        return False
    if selection.outcome_id and row["outcome_id"] != selection.outcome_id:
        return False
    row_project = dict(row).get("project", project_name)
    if selection.project and row_project != selection.project:
        return False
    stamp = row["started_at"] or ""
    if selection.since and stamp < selection.since:
        return False
    return not (selection.until and stamp > selection.until)


def export_traces(
    stores: Iterable[tuple[sqlite3.Connection, str, str | None]],
    output: Path,
    selection: ExportSelection,
    *,
    home: Path | None = None,
    env_values: Iterable[str] | None = None,
) -> dict:
    selection.validate()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing export: {output}")
    rows: dict[str, tuple[sqlite3.Row, str, str | None]] = {}
    # Later stores overlay earlier ones on ID collision, so callers pass the
    # global store first and the project store last (project overlays global).
    for db, scope, project_name in stores:
        for row in db.execute("SELECT * FROM sessions WHERE ended_at IS NOT NULL ORDER BY id"):
            if _selected(row, selection, project_name):
                rows[row["id"]] = (row, scope, project_name)
    missing = sorted(set(selection.session_ids) - set(rows))
    if missing:
        raise ValueError("requested completed sessions not found: " + ", ".join(missing))
    if not rows:
        raise ValueError("scope matched no completed sessions")

    output.mkdir(parents=True)
    manifest_sessions = []
    total_redactions = 0
    try:
        for position, session_id in enumerate(sorted(rows), 1):
            row, scope, project_name = rows[session_id]
            source = Path(row["source_path"] or "")
            if source.is_dir():
                candidates = [source / "transcript.jsonl", source / "claude-code.jsonl"]
                source = next((item for item in candidates if item.is_file()), source)
            if not source.is_file():
                raise FileNotFoundError(f"trace source for {session_id} is unavailable: {source}")
            raw = source.read_bytes()
            records, redactions = [], 0
            for line_no, line in enumerate(raw.decode("utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{source}: invalid JSON at line {line_no}") from exc
                clean, found = redact(record, home, env_values)
                records.append(json.dumps(clean, sort_keys=True, separators=(",", ":")))
                redactions += found
            content = ("\n".join(records) + "\n").encode()
            name = f"session-{position:04d}.jsonl"
            (output / name).write_bytes(content)
            total_redactions += redactions
            manifest_sessions.append({
                "session_id": session_id,
                "outcome_id": row["outcome_id"],
                "project": dict(row).get("project", project_name),
                "scope": scope,
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "file": name,
                "source_sha256": hashlib.sha256(raw).hexdigest(),
                "export_sha256": hashlib.sha256(content).hexdigest(),
                "redaction_count": redactions,
            })
        manifest = {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "format": "stock-claude-code-jsonl",
            "selection": {
                "session_ids": list(selection.session_ids), "outcome_id": selection.outcome_id,
                "project": selection.project, "since": selection.since, "until": selection.until,
            },
            "session_count": len(manifest_sessions),
            "redaction_count": total_redactions,
            "sessions": manifest_sessions,
        }
        (output / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return manifest
    except Exception:
        for path in sorted(output.glob("*")):
            path.unlink()
        output.rmdir()
        raise
