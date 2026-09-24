"""Replay fixture schemas for memory-backend comparison.

Pydantic models for the canonical replay fixture set: trace corpora, typed
notes with source anchors, golden retrieval queries, and declarative
backend-operation scenarios. The checked-in set is the canonical schema and
dev smoke set, NOT the adoption gate: its corpus is generated-dogfood
(scripted agent), and gate eligibility is reserved for fixtures anchored to
held-out real harvested traces. The v1 eval-golden format
(evals.py) converts in through :func:`to_eval_fixture_v1` so the deterministic
CI evaluator can still score the retrieval half.

Fixtures are data;
the phase-checkpointed runner executes them.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

FIXTURE_VERSION = 2
CORPUS_SCHEMA_VERSION = 1


FIXTURE_FAMILIES = (
    "changed-facts",
    "duplication",
    "unsupported-inference",
    "cross-project-leakage",
    "staleness",
    "perspective-leakage",
    "derived-claim-quarantine",
    "source-fidelity",
    "profile-freshness",
    "scope-isolation",
    "export-delete-rebuild",
    "golden-queries",
    "associative-cues",
)


class TraceSessionRef(BaseModel):
    """One session inside the checked-in corpus."""

    session_id: str
    project: str | None = None
    file: str
    export_sha256: str = Field(min_length=64, max_length=64)


class CorpusManifest(BaseModel):
    """A checked-in trace corpus.

    origin="harvested" is reserved for real sessions exported through
    `above-all trace export` (with its redaction rules). The current corpus is
    "generated-dogfood": a scripted stock-Claude-Code agent regenerating the
    lost 2026-09-19 bundle - directional smoke data, never the adoption gate.
    """

    schema_version: int = CORPUS_SCHEMA_VERSION
    format: str
    origin: Literal["harvested", "generated-dogfood", "authored"] = "harvested"
    generator: str
    created: str
    sessions: list[TraceSessionRef]


class FixtureNote(BaseModel):
    """A typed eval note. `source_anchors` tie it back to corpus sessions or docs."""

    id: str
    scope: str = "global"
    title: str
    body: str
    status: str = "active"
    stale_after: str | None = None
    observer: str | None = None
    inference: str | None = None  # set when the note is an inferred conclusion, never observed
    source_anchors: list[str] = Field(default_factory=list)


class ReplayCase(BaseModel):
    """One golden retrieval query, labeled by the kind of cue it exercises."""

    id: str
    cue_type: Literal["descriptive", "associative"] = "descriptive"
    scope: str = "global"
    query: str
    relevant_note_ids: list[str] = Field(default_factory=list)
    stale_note_ids: list[str] = Field(default_factory=list)
    forbidden_scopes: list[str] = Field(default_factory=list)
    expected_facts: list[str] = Field(default_factory=list)
    forbidden_facts: list[str] = Field(default_factory=list)
    expected_abstention: bool = False
    answer: dict[str, Any] | None = None


class ReplayOperation(BaseModel):
    """One scripted backend operation for the behavioral scenario families.

    Executed by the replay runner inside a fresh scope. `expect` fields are
    assertions: an operation whose expectation fails marks the fixture failed
    for that backend.
    """

    op: Literal[
        "create_candidate",  # args: title, body, note_type, sources?, scope?, inference?
        "approve",  # args: candidate_id, replaces?, scope?
        "discard",  # args: candidate_id, scope?
        "search",  # args: query, scope?; expect: returned_note_ids?, forbidden_note_ids?, abstained?
        "generate_context",  # args: scope?; expect: max_bytes?, excluded_note_ids?
        "delete_note",  # args: note_id, scope?
        "rebuild_index",  # no args; expect: searchable_note_ids?, gone_note_ids?
        "export",  # args: scope?; expect: manifest_sessions?
    ]
    args: dict[str, Any] = Field(default_factory=dict)
    expect: dict[str, Any] = Field(default_factory=dict)


class FixtureProvenance(BaseModel):
    """Where the fixture came from.

    "harvested"/"mixed" anchor to a real harvested corpus and are the only
    adoption-gate-eligible origins. "synthetic" anchors to a generated-dogfood
    corpus (schema/dev smoke). "authored" is hand-written.
    """

    origin: Literal["harvested", "synthetic", "authored", "mixed"]
    trace_session_ids: list[str] = Field(default_factory=list)
    generator: str
    created: str
    note: str | None = None


def _lexical_terms(text: str) -> set[str]:
    """Case-folded word tokens used to keep associative cues lexically disjoint."""
    return set(re.findall(r"[a-z0-9]+", text.casefold()))


class ReplayFixture(BaseModel):
    """One version-2 replay fixture: typed notes plus retrieval cases plus ops."""

    version: Literal[2] = FIXTURE_VERSION
    id: str
    family: str
    description: str
    provenance: FixtureProvenance
    notes: list[FixtureNote] = Field(default_factory=list)
    cases: list[ReplayCase] = Field(default_factory=list)
    operations: list[ReplayOperation] = Field(default_factory=list)
    k: int = 5

    @model_validator(mode="after")
    def _check_family_and_content(self) -> ReplayFixture:
        if self.family not in FIXTURE_FAMILIES:
            raise ValueError(f"unknown fixture family {self.family!r}")
        if not self.cases and not self.operations:
            raise ValueError("fixture needs at least one retrieval case or operation")
        if self.provenance.origin in {"harvested", "mixed"} and not self.provenance.trace_session_ids:
            raise ValueError("harvested fixtures must name their source trace sessions")
        note_ids = [n.id for n in self.notes]
        known = set(note_ids)
        notes_by_id = {note.id: note for note in self.notes}
        for case in self.cases:
            for ref in case.relevant_note_ids + case.stale_note_ids:
                if ref not in known:
                    raise ValueError(f"case {case.id!r} references unknown note {ref!r}")
            if case.cue_type == "associative":
                query_terms = _lexical_terms(case.query)
                for ref in case.relevant_note_ids:
                    note = notes_by_id[ref]
                    overlap = query_terms & _lexical_terms(f"{note.title} {note.body}")
                    if overlap:
                        raise ValueError(
                            f"associative case {case.id!r} has lexical overlap with {ref!r}: "
                            f"{sorted(overlap)}"
                        )
        return self


class FixtureSetEntry(BaseModel):
    file: str
    sha256: str = Field(min_length=64, max_length=64)
    family: str
    # Gate eligibility is earned, never defaulted: only fixtures anchored to a
    # harvested real-trace corpus may be marked (enforced in validate_fixture_set).
    adoption_gate: bool = False


class FixtureSet(BaseModel):
    """Index of the canonical fixture dir, with checksums for tamper evidence."""

    version: Literal[2] = FIXTURE_VERSION
    name: str
    corpus: str | None = None  # relative path of the corpus manifest, when one backs the set
    fixtures: list[FixtureSetEntry]
    policy: str = (
        "This set is the canonical schema and dev smoke set for MemoryBackend comparison, "
        "NOT the adoption gate. The adoption decision stays null/blocked until held-out "
        "real harvested traces arrive and back fixtures marked "
        "adoption_gate=true; public benchmark and dogfood runs are smoke tests only."
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_replay_fixture(path: Path) -> ReplayFixture:
    return ReplayFixture.model_validate(json.loads(path.read_text(encoding="utf-8")))


def load_corpus_manifest(path: Path) -> CorpusManifest:
    return CorpusManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))


def validate_fixture_set(root: Path) -> FixtureSet:
    """Validate the set index, every fixture file, checksums, and corpus anchors."""
    index = FixtureSet.model_validate(json.loads((root / "fixture-set.json").read_text(encoding="utf-8")))
    corpus_sessions: set[str] = set()
    if index.corpus:
        corpus = load_corpus_manifest(root / index.corpus)
        corpus_root = (root / index.corpus).parent
        for session in corpus.sessions:
            if _sha256(corpus_root / session.file) != session.export_sha256:
                raise ValueError(f"corpus session {session.session_id} fails its sha256")
            corpus_sessions.add(session.session_id)
    seen_families: set[str] = set()
    for entry in index.fixtures:
        fixture_path = root / entry.file
        if _sha256(fixture_path) != entry.sha256:
            raise ValueError(f"fixture {entry.file} fails its sha256")
        fixture = load_replay_fixture(fixture_path)
        if fixture.family != entry.family:
            raise ValueError(f"fixture {entry.file} family mismatch: {fixture.family} != {entry.family}")
        if fixture.provenance.trace_session_ids and corpus_sessions:
            unknown = set(fixture.provenance.trace_session_ids) - corpus_sessions
            if unknown:
                raise ValueError(f"fixture {entry.file} anchors to unknown sessions: {sorted(unknown)}")
        if entry.adoption_gate:
            corpus_origin = corpus.origin if index.corpus else None
            if corpus_origin != "harvested" or fixture.provenance.origin not in {"harvested", "mixed"}:
                raise ValueError(
                    f"fixture {entry.file} is marked adoption_gate=true but is not gate-eligible: "
                    "only harvested/mixed fixtures anchored to a harvested real-trace corpus qualify "
                    f"(corpus origin={corpus_origin!r}, fixture origin={fixture.provenance.origin!r})"
                )
        seen_families.add(entry.family)
    missing = set(FIXTURE_FAMILIES) - seen_families
    if missing:
        raise ValueError(f"fixture set is missing families: {sorted(missing)}")
    return index


def build_fixture_set_index(root: Path, *, name: str, corpus: str | None) -> FixtureSet:
    """(Re)generate fixture-set.json from the fixture files present under root."""
    entries = []
    for path in sorted((root / "fixtures").glob("*.json")):
        fixture = load_replay_fixture(path)
        entries.append(
            FixtureSetEntry(
                file=f"fixtures/{path.name}",
                sha256=_sha256(path),
                family=fixture.family,
            )
        )
    index = FixtureSet(version=FIXTURE_VERSION, name=name, corpus=corpus, fixtures=entries)
    (root / "fixture-set.json").write_text(index.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return index


def to_eval_fixture_v1(fixture: ReplayFixture) -> dict[str, Any]:
    """Drop a v2 fixture to the v1 eval-golden shape that evals.py scores."""
    if fixture.operations:
        raise ValueError("operation fixtures need the replay runner; v1 covers retrieval cases only")
    return {
        "version": 1,
        "k": fixture.k,
        "notes": [
            {
                "id": n.id,
                "scope": n.scope,
                "title": n.title,
                "body": n.body,
                "status": n.status,
                **({"stale_after": n.stale_after} if n.stale_after else {}),
            }
            for n in fixture.notes
        ],
        "cases": [c.model_dump(exclude_none=True) for c in fixture.cases],
    }
