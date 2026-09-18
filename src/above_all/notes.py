from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

import yaml

REQUIRED = {"type", "sources", "generated", "verified", "status", "stale_after"}


@dataclass(frozen=True)
class Note:
    metadata: dict
    body: str

    @property
    def stale(self) -> bool:
        value = self.metadata.get("stale_after")
        if not value:
            return False
        cutoff = value if isinstance(value, date) else date.fromisoformat(str(value))
        return cutoff < datetime.now(timezone.utc).date()


def parse_note(text: str) -> Note:
    match = re.fullmatch(r"---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if not match:
        raise ValueError("note must contain YAML front matter")
    metadata = yaml.safe_load(match.group(1)) or {}
    missing = REQUIRED - metadata.keys()
    if missing:
        raise ValueError(f"missing required fields: {', '.join(sorted(missing))}")
    if not isinstance(metadata["sources"], list) or not metadata["sources"]:
        raise ValueError("sources must be a non-empty list")
    return Note(metadata, match.group(2).strip())


def create_note(
    notes_dir: Path,
    title: str,
    body: str,
    note_type: str,
    sources: list[str],
    stale_after: str | None = None,
) -> Path:
    notes_dir.mkdir(parents=True, exist_ok=True)
    note_id = uuid4().hex
    metadata = {
        "type": note_type,
        "sources": sources,
        "generated": datetime.now(timezone.utc).isoformat(),
        "verified": "none",
        "status": "draft",
        "stale_after": stale_after,
    }
    path = notes_dir / f"{note_id}.md"
    front = yaml.safe_dump(metadata, sort_keys=False).strip()
    path.write_text(f"---\n{front}\n---\n# {title}\n\n{body.strip()}\n", encoding="utf-8")
    return path


def index_note(db, path: Path) -> str:
    note = parse_note(path.read_text(encoding="utf-8"))
    note_id = path.stem
    title = next(
        (line[2:].strip() for line in note.body.splitlines() if line.startswith("# ")),
        note_id,
    )
    now = datetime.now(timezone.utc).isoformat()
    with db:
        db.execute("DELETE FROM notes_fts WHERE note_id=?", (note_id,))
        db.execute(
            "INSERT OR REPLACE INTO notes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                note_id,
                str(path),
                note.metadata["type"],
                json.dumps(note.metadata["sources"]),
                str(note.metadata["generated"]),
                note.metadata["verified"],
                note.metadata["status"],
                str(note.metadata["stale_after"]) if note.metadata["stale_after"] else None,
                title,
                note.body,
                now,
            ),
        )
        if note.metadata["status"] == "active" and not note.stale:
            db.execute(
                "INSERT INTO notes_fts(note_id,title,body) VALUES (?,?,?)",
                (note_id, title, note.body),
            )
    return note_id


def search_notes(db, query: str) -> list[dict]:
    today = datetime.now(timezone.utc).date().isoformat()
    rows = db.execute(
        "SELECT n.id,n.title,n.body,n.path FROM notes_fts f "
        "JOIN notes n ON n.id=f.note_id WHERE notes_fts MATCH ? "
        "AND n.status='active' AND (n.stale_after IS NULL OR n.stale_after >= ?) "
        "ORDER BY bm25(notes_fts)",
        (query, today),
    )
    return [dict(row) for row in rows]



def render_note(metadata: dict, body: str) -> str:
    front = yaml.safe_dump(metadata, sort_keys=False).strip()
    return f"---\n{front}\n---\n{body.strip()}\n"


def _write_note(path: Path, metadata: dict, body: str) -> Path:
    path.write_text(render_note(metadata, body), encoding="utf-8")
    return path


def set_note_status(path: Path, status: str, extra: dict | None = None) -> Path:
    note = parse_note(path.read_text(encoding="utf-8"))
    metadata = dict(note.metadata)
    metadata["status"] = status
    if extra:
        metadata.update(extra)
    return _write_note(path, metadata, note.body)


def create_candidate(
    candidates_dir: Path,
    title: str,
    body: str,
    note_type: str,
    sources: list[str],
    stale_after: str | None = None,
    extra: dict | None = None,
) -> Path:
    """Create a candidate note. Candidates are drafts awaiting review, never active memory."""
    candidates_dir.mkdir(parents=True, exist_ok=True)
    note_id = uuid4().hex
    metadata = {
        "type": note_type,
        "sources": sources,
        "generated": datetime.now(timezone.utc).isoformat(),
        "verified": "none",
        "status": "candidate",
        "stale_after": stale_after,
    }
    if extra:
        metadata.update(extra)
    return _write_note(candidates_dir / f"{note_id}.md", metadata, f"# {title}\n\n{body}")


def list_candidates(candidates_dir: Path) -> list[dict]:
    if not candidates_dir.is_dir():
        return []
    found = []
    for path in sorted(candidates_dir.glob("*.md")):
        try:
            note = parse_note(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if note.metadata["status"] != "candidate":
            continue
        title = next(
            (line[2:].strip() for line in note.body.splitlines() if line.startswith("# ")),
            path.stem,
        )
        found.append(
            {"id": path.stem, "title": title, "generated": str(note.metadata["generated"])}
        )
    return found


def active_notes(db, limit: int = 20) -> list[dict]:
    today = datetime.now(timezone.utc).date().isoformat()
    rows = db.execute(
        "SELECT id,title,body,path FROM notes WHERE status='active' "
        "AND (stale_after IS NULL OR stale_after >= ?) ORDER BY indexed_at DESC LIMIT ?",
        (today, limit),
    )
    return [dict(row) for row in rows]


def approve_candidate(db, scope_dir: Path, candidate_id: str, replaces: str | None = None) -> Path:
    """Promote a candidate to active memory; optionally supersede the note it replaces.

    Replacement keeps the prior note on disk with its source chain as superseded -
    replace, never silent append-and-contradict.
    """
    source = scope_dir / "candidates" / f"{candidate_id}.md"
    if not source.is_file():
        raise ValueError(f"no candidate named {candidate_id!r}")
    note = parse_note(source.read_text(encoding="utf-8"))
    if note.metadata["status"] != "candidate":
        raise ValueError(f"note {candidate_id!r} is {note.metadata['status']}, not a candidate")
    if replaces:
        old = scope_dir / "notes" / f"{replaces}.md"
        if not old.is_file():
            raise ValueError(f"no note named {replaces!r} to replace")
        set_note_status(old, "superseded")
        index_note(db, old)
    target = scope_dir / "notes" / f"{candidate_id}.md"
    metadata = dict(note.metadata)
    metadata["status"] = "active"
    if replaces:
        metadata["replaces"] = f"note:{replaces}"
    _write_note(target, metadata, note.body)
    source.unlink()
    index_note(db, target)
    return target


def discard_candidate(scope_dir: Path, candidate_id: str) -> Path:
    source = scope_dir / "candidates" / f"{candidate_id}.md"
    if not source.is_file():
        raise ValueError(f"no candidate named {candidate_id!r}")
    return set_note_status(source, "rejected")
