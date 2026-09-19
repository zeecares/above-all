"""Memory backend interface.

The knowledge plane hides behind a small interface so the reference backend
(SQLite FTS5 over Markdown notes) can later be benchmarked against a pluggable
candidate such as mem0_oss on harvested traces. Only backends registered here
may be selected; an unknown name fails loudly.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Protocol

from . import notes as _notes


class MemoryBackend(Protocol):
    """Admission, retrieval, and review operations for the knowledge plane."""

    name: str

    def create_candidate(
        self,
        scope_dir: Path,
        *,
        title: str,
        body: str,
        note_type: str,
        sources: list[str],
        stale_after: str | None = None,
        extra: dict | None = None,
        candidate_id: str | None = None,
    ) -> Path:
        """Create a candidate note. Candidates are never active memory."""
        ...

    def search(self, db: sqlite3.Connection, query: str) -> list[dict]:
        """Ranked search over active, non-stale notes only."""
        ...

    def active_notes(self, db: sqlite3.Connection, limit: int = 20) -> list[dict]:
        """Active, non-stale notes, most recently indexed first."""
        ...

    def approve(
        self, db: sqlite3.Connection, scope_dir: Path, candidate_id: str, replaces: str | None = None, contradicts: list[str] | None = None
    ) -> Path:
        """Promote a candidate to active memory, optionally superseding a note."""
        ...

    def discard(self, scope_dir: Path, candidate_id: str) -> Path:
        """Reject a candidate. The file is kept for audit with status rejected."""
        ...


class SqliteFtsBackend:
    """Reference backend: Markdown files plus an FTS5 index in the scope database."""

    name = "sqlite_fts"

    def create_candidate(self, scope_dir, *, title, body, note_type, sources, stale_after=None, extra=None, candidate_id=None):
        return _notes.create_candidate(
            scope_dir / "candidates",
            title,
            body,
            note_type,
            sources,
            stale_after=stale_after,
            extra=extra,
            candidate_id=candidate_id,
        )

    def search(self, db, query):
        return _notes.search_notes(db, query)

    def active_notes(self, db, limit=20):
        return _notes.active_notes(db, limit)

    def approve(self, db, scope_dir, candidate_id, replaces=None, contradicts=None):
        from .write_gate import promote_candidate

        return promote_candidate(db, scope_dir, candidate_id, replaces=replaces, contradicts=contradicts)

    def discard(self, scope_dir, candidate_id):
        return _notes.discard_candidate(scope_dir, candidate_id)


class SqliteHybridBackend(SqliteFtsBackend):
    """Opt-in BM25 plus local hashed n-gram embeddings, merged by RRF."""

    name = "sqlite_hybrid"

    def search(self, db, query):
        from .hybrid import reciprocal_rank_fusion, semantic_rows

        return reciprocal_rank_fusion(_notes.search_notes(db, query), semantic_rows(db, query))


_BACKENDS = {"sqlite_fts": SqliteFtsBackend, "sqlite_hybrid": SqliteHybridBackend}


def get_backend(name: str | None = None) -> MemoryBackend:
    selected = name or "sqlite_fts"
    if selected not in _BACKENDS:
        available = ", ".join(sorted(_BACKENDS))
        raise ValueError(
            f"unknown memory backend {selected!r} (available: {available}; "
            "mem0_oss is a later pluggable candidate pending trace evals - see spec/memory.md)"
        )
    return _BACKENDS[selected]()
