"""The single, recoverable write gate for promoting memory candidates."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from .notes import index_note, parse_note, render_note


def _claim(body: str) -> str:
    lines = body.strip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).strip().casefold()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    staged.write_bytes(data)
    os.replace(staged, path)


def _journal_write(path: Path, payload: dict) -> None:
    _atomic_write(path, (json.dumps(payload, sort_keys=True) + "\n").encode())


def _restore(db: sqlite3.Connection, payload: dict, *, commit: bool = True) -> None:
    for item in payload["files"]:
        path = Path(item["path"])
        before = item.get("before")
        if before is None:
            path.unlink(missing_ok=True)
        else:
            _atomic_write(path, bytes.fromhex(before))
    for item in payload["files"]:
        path = Path(item["path"])
        if path.parent.name != "notes":
            continue
        if path.exists():
            index_note(db, path)
        else:
            note_id = path.stem
            with db:
                db.execute("DELETE FROM notes_fts WHERE note_id=?", (note_id,))
                db.execute("DELETE FROM notes WHERE id=?", (note_id,))
    if commit:
        db.commit()


def recover_promotions(db: sqlite3.Connection, scope_dir: Path) -> list[str]:
    """Rollback incomplete promotions and rebuild their index rows."""
    recovered = []
    root = scope_dir / "transactions"
    for journal in sorted(root.glob("promotion-*.json")):
        payload = json.loads(journal.read_text())
        _restore(db, payload)
        journal.unlink()
        recovered.append(payload["candidate_id"])
    return recovered


def promote_candidate(
    db: sqlite3.Connection,
    scope_dir: Path,
    candidate_id: str,
    replaces: str | None = None,
    contradicts: list[str] | None = None,
    *,
    fault: Callable[[str], None] | None = None,
) -> Path:
    """Revalidate and promote with an on-disk recovery marker around DB/files."""
    # Serialize recovery and revalidation with every other promotion using the same memory DB.
    # Without taking the write lock before reading active memory, two processes can
    # both validate the same claim and then promote it.
    db.execute("BEGIN IMMEDIATE")
    journal: Path | None = None
    journal_payload: dict | None = None
    try:
        root = scope_dir / "transactions"
        for stale_journal in sorted(root.glob("promotion-*.json")):
            stale_payload = json.loads(stale_journal.read_text())
            _restore(db, stale_payload, commit=False)
            stale_journal.unlink()
        source = scope_dir / "candidates" / f"{candidate_id}.md"
        if not source.is_file():
            raise ValueError(f"no candidate named {candidate_id!r}")
        candidate = parse_note(source.read_text())
        if candidate.metadata["status"] not in {"candidate", "reviewed"}:
            raise ValueError(f"note {candidate_id!r} is not promotable")

        active = db.execute("SELECT id,body,path FROM notes WHERE status='active'").fetchall()
        duplicate = next(
            (row[0] for row in active if _claim(row[1]) == _claim(candidate.body)), None
        )
        if duplicate:
            raise ValueError(f"candidate duplicates active note {duplicate!r}; returned to review")
        old = None
        if replaces:
            old = next((Path(row[2]) for row in active if row[0] == replaces), None)
            if old is None or not old.is_file():
                raise ValueError(f"no active note named {replaces!r} to replace")
        active_ids = {row[0] for row in active}
        missing_contradictions = [
            item.removeprefix("note:") for item in (contradicts or [])
            if item.removeprefix("note:") not in active_ids
        ]
        if missing_contradictions:
            raise ValueError(
                "contradiction targets must be active notes: "
                + ", ".join(sorted(missing_contradictions))
            )

        metadata = dict(candidate.metadata)
        metadata["status"] = "active"
        if replaces:
            metadata["replaces"] = f"note:{replaces}"
        if contradicts:
            metadata["contradicts"] = [f"note:{item.removeprefix('note:')}" for item in contradicts]
        target = scope_dir / "notes" / f"{candidate_id}.md"
        files = [source, target] + ([old] if old else [])
        journal_payload = {
            "candidate_id": candidate_id,
            "files": [
                {"path": str(path), "before": path.read_bytes().hex() if path.exists() else None}
                for path in files
            ],
        }
        journal = scope_dir / "transactions" / f"promotion-{uuid4().hex}.json"
        _journal_write(journal, journal_payload)
        if fault:
            fault("journal")
        if old:
            parsed_old = parse_note(old.read_text())
            old_meta = dict(parsed_old.metadata)
            old_meta["status"] = "superseded"
            _atomic_write(old, render_note(old_meta, parsed_old.body).encode())
            if fault:
                fault("old_file")
        _atomic_write(target, render_note(metadata, candidate.body).encode())
        if fault:
            fault("new_file")
        if old:
            index_note(db, old)
        index_note(db, target)
        if fault:
            fault("db")
        db.commit()
        if fault:
            fault("commit")
        source.unlink()
        journal.unlink()
        return target
    except BaseException:
        if db.in_transaction:
            db.rollback()
        if journal_payload is not None:
            _restore(db, journal_payload)
        if journal is not None:
            journal.unlink(missing_ok=True)
        raise
