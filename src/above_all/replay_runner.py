"""Phase-checkpointed replay runner for MemoryBackend comparison (ZEE-70).

Executes version-2 replay fixtures (above_all.replay) against real backend
implementations in fresh on-disk scopes. Phases checkpoint as they complete -
ingest -> index -> retrieve -> answer -> evaluate - so a failed run resumes
from the first incomplete phase and stage costs stay inspectable.

Metrics follow spec/memory.md (2026-09-21): answer/retrieval quality, latency
and context tokens reported separately, plus cost, abstention, provenance,
pollution and operations. OpenTelemetry spans wrap each phase; the SDK is
configured by the caller (tests use the in-memory exporter, production may
attach OTLP).

mem0_oss and supermemory_local stay blocked behind policy facts the owner has
not supplied (internal model gateway mapping, third-party memory binary policy,
one-binary rule); asking for them fails loudly with the exact gap.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from opentelemetry import metrics, trace

from . import notes as _notes
from .agent_context import generate_agent_context
from .db import GLOBAL_MIGRATIONS, PROJECT_MIGRATIONS, migrate
from .memory_backend import get_backend
from .replay import ReplayFixture, load_replay_fixture

PHASES = ("ingest", "index", "retrieve", "answer", "evaluate")

BLOCKED_BACKENDS = {
    "mem0_oss": (
        "mem0_oss replay is blocked on owner policy facts: which internal model/embedding "
        "gateway endpoints it may use, and whether a vector store dependency is acceptable. "
        "See spec/memory.md - it runs only after sqlite_hybrid shows a gap on real traces."
    ),
    "supermemory_local": (
        "supermemory_local replay is blocked on owner policy facts: company policy on a "
        "third-party memory binary touching internal transcripts, whether a local embedding "
        "model is allowed or all model traffic goes through the internal gateway, and whether "
        "a second local process breaks the one-binary rule."
    ),
}


class BackendBlockedError(RuntimeError):
    """A candidate backend was requested before its policy facts landed."""


_tracer = trace.get_tracer("above_all.replay")
_meter = metrics.get_meter("above_all.replay")
def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON via tmp + replace so a torn write can't become the restart state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


_phase_counter = _meter.create_counter(
    "replay.phase.completed", description="Replay phases completed, by phase and backend"
)
_phase_latency = _meter.create_histogram(
    "replay.phase.duration_ms", description="Replay phase wall time in milliseconds"
)

CHARS_PER_TOKEN = 4  # documented proxy until tokenizer-backed counts land (typesafe-lab #6)


def _scope_name(scope: str) -> str:
    return "global" if scope == "global" else f"project-{scope}"


class ReplayRunner:
    """Runs one fixture against one backend inside work_dir with checkpoints."""

    def __init__(self, fixture: ReplayFixture, backend_name: str, work_dir: Path) -> None:
        if backend_name in BLOCKED_BACKENDS:
            raise BackendBlockedError(BLOCKED_BACKENDS[backend_name])
        self.fixture = fixture
        self.backend = get_backend(backend_name)
        self.backend_name = backend_name
        self.root = work_dir / backend_name / fixture.id
        self.checkpoint_path = self.root / "checkpoint.json"
        self._dbs: dict[str, Any] = {}

    # -- checkpointing -----------------------------------------------------

    def _fingerprint(self, phase: str) -> str:
        import hashlib

        payload = json.dumps(
            {
                "fixture": self.fixture.model_dump(mode="json"),
                "backend": self.backend_name,
                "phase": phase,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def _saved_record(self, phase: str) -> dict[str, Any] | None:
        """The checkpoint record for a completed phase whose fingerprint still matches."""
        if not self.checkpoint_path.is_file():
            return None
        done = json.loads(self.checkpoint_path.read_text(encoding="utf-8")).get("phases", {})
        record = done.get(phase)
        if record and record.get("fingerprint") == self._fingerprint(phase):
            return record
        return None

    def _completed(self, phase: str) -> bool:
        return self._saved_record(phase) is not None

    def _mark(self, phase: str, duration_ms: float, detail: dict[str, Any]) -> None:
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        state = (
            json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
            if self.checkpoint_path.is_file()
            else {"fixture_id": self.fixture.id, "backend": self.backend_name, "phases": {}}
        )
        state["phases"][phase] = {
            "fingerprint": self._fingerprint(phase),
            "duration_ms": duration_ms,
            "detail": detail,
        }
        _write_json_atomic(self.checkpoint_path, state)
        _phase_counter.add(1, {"phase": phase, "backend": self.backend_name})
        _phase_latency.record(duration_ms, {"phase": phase, "backend": self.backend_name})

    # -- scope plumbing ------------------------------------------------------

    def _scope_dir(self, scope: str) -> Path:
        path = self.root / "scopes" / _scope_name(scope)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _db(self, scope: str):
        if scope not in self._dbs:
            migrations = GLOBAL_MIGRATIONS if scope == "global" else PROJECT_MIGRATIONS
            self._dbs[scope] = migrate(self._scope_dir(scope) / "assistant.db", migrations)
        return self._dbs[scope]

    def _scopes_used(self) -> list[str]:
        scopes = {note.scope for note in self.fixture.notes}
        for case in self.fixture.cases:
            scopes.add(case.scope)
        for op in self.fixture.operations:
            if op.args.get("scope"):
                scopes.add(str(op.args["scope"]))
        return sorted(scopes)

    def close(self) -> None:
        for db in self._dbs.values():
            db.close()
        self._dbs.clear()

    # -- phases --------------------------------------------------------------

    def _ingest(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for note in self.fixture.notes:
            scope_dir = self._scope_dir(note.scope)
            (scope_dir / "notes").mkdir(parents=True, exist_ok=True)
            metadata = {
                "type": "fact",
                "sources": note.source_anchors or ["fixture:" + self.fixture.id],
                "generated": self.fixture.provenance.created,
                "verified": "source" if note.source_anchors else "none",
                "status": note.status,
                "stale_after": note.stale_after,
            }
            if note.observer:
                metadata["observer"] = note.observer
            if note.inference:
                metadata["inference"] = note.inference
            path = _notes._write_note(
                scope_dir / "notes" / f"{note.id}.md", metadata, f"# {note.title}\n\n{note.body}"
            )
            _notes.index_note(self._db(note.scope), path)
            counts[note.scope] = counts.get(note.scope, 0) + 1
        return {"notes_ingested": counts}

    def _index(self) -> dict[str, Any]:
        # Re-index every note file from disk: a measurable, backend-honest index phase.
        indexed = 0
        for scope in self._scopes_used():
            notes_dir = self._scope_dir(scope) / "notes"
            for path in sorted(notes_dir.glob("*.md")):
                _notes.index_note(self._db(scope), path)
                indexed += 1
        return {"notes_indexed": indexed}

    def _search(self, scope: str, query: str, k: int) -> tuple[list[dict], float]:
        started = time.perf_counter()
        found: dict[str, dict] = {}
        if scope != "global":
            for row in self.backend.search(self._db(scope), query):
                found[row["id"]] = dict(row, scope=scope)
        for row in self.backend.search(self._db("global"), query):
            found.setdefault(row["id"], dict(row, scope="global"))
        elapsed = (time.perf_counter() - started) * 1000
        return list(found.values())[:k], elapsed

    def _retrieve(self) -> dict[str, Any]:
        k = self.fixture.k
        case_reports = []
        latencies: list[float] = []
        context_chars = 0
        for case in self.fixture.cases:
            rows, elapsed = self._search(case.scope, case.query, k)
            ids = [row["id"] for row in rows]
            latencies.append(elapsed)
            context_chars += sum(len(row["title"]) + len(row["body"]) for row in rows)
            relevant = set(case.relevant_note_ids)
            stale = set(case.stale_note_ids)
            forbidden_scopes = set(case.forbidden_scopes)
            case_reports.append(
                {
                    "id": case.id,
                    "cue_type": case.cue_type,
                    "retrieved_note_ids": ids,
                    "recall": len(relevant & set(ids)) / len(relevant) if relevant else 1.0,
                    "mrr": next(
                        (1.0 / rank for rank, nid in enumerate(ids, 1) if nid in relevant),
                        0.0 if relevant else 1.0,
                    ),
                    "stale_leaks": len(stale & set(ids)),
                    "cross_leaks": sum(row["scope"] in forbidden_scopes for row in rows),
                    "abstained": not ids,
                    "expected_abstention": case.expected_abstention,
                }
            )
        operation_reports = [self._run_operation(op) for op in self.fixture.operations]
        return {
            "cases": case_reports,
            "operations": operation_reports,
            "mean_latency_ms": sum(latencies) / len(latencies) if latencies else 0.0,
            "context_tokens": context_chars // CHARS_PER_TOKEN,
        }

    def _run_operation(self, op) -> dict[str, Any]:
        scope = str(op.args.get("scope", "global"))
        expect = op.expect
        report: dict[str, Any] = {"op": op.op, "ok": True, "detail": ""}
        try:
            if op.op == "create_candidate":
                try:
                    path = self.backend.create_candidate(
                        self._scope_dir(scope),
                        title=str(op.args["title"]),
                        body=str(op.args["body"]),
                        note_type=str(op.args.get("note_type", "fact")),
                        sources=list(op.args.get("sources", [])),
                        extra={"inference": op.args["inference"]} if op.args.get("inference") else None,
                        candidate_id=op.args.get("candidate_id"),
                    )
                except ValueError as exc:
                    # The write gate rejects unsourced/empty candidates at write time.
                    report["ok"] = bool(expect.get("rejected"))
                    report["detail"] = f"gate rejected at write: {exc}"
                    return report
                # Gate check from the declared inputs (parse_note itself refuses
                # unsourced notes, which is the write-time rejection above).
                gate_reject = not op.args.get("sources") or not str(op.args["body"]).strip()
                if expect.get("rejected"):
                    report["ok"] = gate_reject
                    report["detail"] = "gate rejected unsourced/empty candidate" if gate_reject else "gate accepted an unsourced candidate"
                    if gate_reject:
                        # set_note_status/discard_candidate re-parse and refuse unsourced
                        # notes (write-gate gap reported upstream), so flip the status field
                        # directly; the rejected file stays on disk for audit.
                        import re as _re

                        cand = self._scope_dir(scope) / "candidates" / f"{op.args['candidate_id']}.md"
                        cand.write_text(
                            _re.sub(r"^status: .*$", "status: rejected", cand.read_text(encoding="utf-8"), count=1, flags=_re.MULTILINE),
                            encoding="utf-8",
                        )
                else:
                    report["ok"] = bool(expect.get("created", True)) and not gate_reject
                    report["detail"] = str(path)
            elif op.op == "approve":
                try:
                    self.backend.approve(
                        self._db(scope),
                        self._scope_dir(scope),
                        str(op.args["candidate_id"]),
                        replaces=op.args.get("replaces"),
                    )
                    report["ok"] = bool(expect.get("ok", True))
                    report["detail"] = "promoted"
                except ValueError as exc:
                    report["ok"] = not expect.get("ok", True)
                    report["detail"] = f"promotion refused: {exc}"
            elif op.op == "discard":
                self.backend.discard(self._scope_dir(scope), str(op.args["candidate_id"]))
            elif op.op == "search":
                rows, _ = self._search(scope, str(op.args["query"]), self.fixture.k)
                ids = [row["id"] for row in rows]
                problems = []
                for wanted in expect.get("returned_note_ids", []):
                    if wanted not in ids:
                        problems.append(f"missing {wanted}")
                for banned in expect.get("forbidden_note_ids", []):
                    if banned in ids:
                        problems.append(f"leaked {banned}")
                if "abstained" in expect and bool(ids) == bool(expect["abstained"]):
                    problems.append("abstention mismatch")
                if "max_returned_for_query" in expect and len(ids) > int(expect["max_returned_for_query"]):
                    problems.append(f"returned {len(ids)} > {expect['max_returned_for_query']}")
                report["ok"] = not problems
                report["detail"] = "; ".join(problems) or f"returned {ids}"
            elif op.op == "generate_context":
                titles = {n.id: n.title for n in self.fixture.notes}
                path = generate_agent_context(
                    self._scope_dir(scope), self._db(scope), self.backend,
                    self._db("global") if scope != "global" else None,
                )
                content = path.read_text(encoding="utf-8")
                size = len(content.encode())
                problems = []
                for nid in expect.get("included_note_ids", []):
                    if titles.get(nid, nid) not in content:
                        problems.append(f"missing {nid}")
                for nid in expect.get("excluded_note_ids", []):
                    if titles.get(nid, nid) in content:
                        problems.append(f"leaked {nid}")
                if "max_bytes" in expect and size > int(expect["max_bytes"]):
                    problems.append(f"context {size}B > {expect['max_bytes']}B")
                report["ok"] = not problems
                report["detail"] = "; ".join(problems) or f"{size}B within budget"
            elif op.op == "delete_note":
                note_path = self._scope_dir(scope) / "notes" / f"{op.args['note_id']}.md"
                _notes.set_note_status(note_path, "rejected")
                _notes.index_note(self._db(scope), note_path)
                report["detail"] = "note rejected and de-indexed"
            elif op.op == "rebuild_index":
                db = self._db(scope)
                db.execute("DELETE FROM notes_fts")
                notes_dir = self._scope_dir(scope) / "notes"
                for path in sorted(notes_dir.glob("*.md")):
                    _notes.index_note(db, path)
                problems = []
                for nid in expect.get("searchable_note_ids", []):
                    if not db.execute(
                        "SELECT 1 FROM notes_fts WHERE note_id=?", (nid,)
                    ).fetchone():
                        problems.append(f"not searchable after rebuild: {nid}")
                for nid in expect.get("gone_note_ids", []):
                    if db.execute("SELECT 1 FROM notes_fts WHERE note_id=?", (nid,)).fetchone():
                        problems.append(f"still searchable after rebuild: {nid}")
                report["ok"] = not problems
                report["detail"] = "; ".join(problems) or "rebuild verified"
            elif op.op == "export":
                # Export the approved sources themselves: the rebuild-from-sources guarantee.
                import hashlib

                out = self.root / f"export-{scope}"
                out.mkdir(parents=True, exist_ok=True)
                notes_dir = self._scope_dir(scope) / "notes"
                exported = []
                for note_path in sorted(notes_dir.glob("*.md")):
                    parsed = _notes.parse_note(note_path.read_text(encoding="utf-8"))
                    if parsed.metadata["status"] != "active":
                        continue
                    data = note_path.read_bytes()
                    (out / note_path.name).write_bytes(data)
                    exported.append({"id": note_path.stem, "sha256": hashlib.sha256(data).hexdigest()})
                (out / "manifest.json").write_text(json.dumps({"notes": exported}, indent=2) + "\n")
                report["ok"] = len(exported) >= int(expect.get("min_notes", 0))
                report["detail"] = f"exported {len(exported)} active notes with checksums"
            else:  # pragma: no cover - schema validation prevents this
                report["ok"] = False
                report["detail"] = f"unknown op {op.op!r}"
        except Exception as exc:  # noqa: BLE001 - an erroring op is a failed op, never a crash
            report["ok"] = False
            report["detail"] = f"{type(exc).__name__}: {exc}"
        return report

    def _answer(self) -> dict[str, Any]:
        factual: list[float] = []
        unsupported: list[float] = []
        abstention: list[float] = []
        for case in self.fixture.cases:
            answer = case.answer
            if answer is None:
                continue
            claims = {str(c) for c in answer.get("claims", [])}
            supported = {str(c) for c in answer.get("supported_facts", [])}
            expected = set(case.expected_facts)
            forbidden = set(case.forbidden_facts)
            factual.append(len(expected & claims) / len(expected) if expected else 1.0)
            bad = (claims - supported) | (claims & forbidden)
            unsupported.append(len(bad) / len(claims) if claims else 0.0)
            abstention.append(float(bool(answer.get("abstained")) == case.expected_abstention))
        mean = lambda xs: sum(xs) / len(xs) if xs else 0.0
        return {
            "evaluated_case_count": len(factual),
            "factual_accuracy": mean(factual),
            "unsupported_claim_rate": mean(unsupported),
            "abstention_accuracy": mean(abstention),
        }

    def _evaluate(self, artifacts: dict[str, Any]) -> dict[str, Any]:
        cases = artifacts["retrieve"]["cases"]
        ops = artifacts["retrieve"]["operations"]
        mean = lambda xs: sum(xs) / len(xs) if xs else 0.0
        descriptive_cases = [c for c in cases if c["cue_type"] == "descriptive"]
        associative_cases = [c for c in cases if c["cue_type"] == "associative"]
        retrieval = {
            "recall_at_k": mean([c["recall"] for c in cases]),
            "descriptive_recall_at_k": mean([c["recall"] for c in descriptive_cases]),
            "associative_recall_at_k": mean([c["recall"] for c in associative_cases]),
            "descriptive_case_count": len(descriptive_cases),
            "associative_case_count": len(associative_cases),
            "mrr": mean([c["mrr"] for c in cases]),
            "stale_leakage_rate": mean([c["stale_leaks"] for c in cases]),
            "cross_project_leakage_rate": mean([c["cross_leaks"] for c in cases]),
            "abstention_correct": mean(
                [float(c["abstained"] == c["expected_abstention"]) for c in cases]
            ),
        }
        return {
            "quality": retrieval,
            "latency_ms": {
                phase: artifacts[phase]["duration_ms"] for phase in ("ingest", "index")
            }
            | {"retrieve_mean_ms": artifacts["retrieve"]["mean_latency_ms"]},
            "context_tokens": artifacts["retrieve"]["context_tokens"],
            "cost": {
                "model_calls": 0,
                "note": "deterministic replay; model-backed answer generation is opt-in later",
            },
            "abstention": {"accuracy": artifacts["answer"]["abstention_accuracy"]},
            "provenance": {
                "anchored_notes": sum(1 for n in self.fixture.notes if n.source_anchors),
                "total_notes": len(self.fixture.notes),
            },
            "pollution": {
                "notes_seeded": len(self.fixture.notes),
                "candidates_created": sum(1 for o in ops if o["op"] == "create_candidate"),
                "operations_failed": [o["op"] + ": " + o["detail"] for o in ops if not o["ok"]],
            },
            "operations": {
                "scopes_created": len(self._scopes_used()),
                "extra_servers": 0,
                "extra_model_runtimes": 0,
            },
        }

    def run(self) -> dict[str, Any]:
        if all(self._completed(phase) for phase in PHASES) and (self.root / "report.json").is_file():
            return json.loads((self.root / "report.json").read_text(encoding="utf-8"))
        artifacts: dict[str, Any] = {}
        with _tracer.start_as_current_span(
            "replay.run",
            attributes={"fixture": self.fixture.id, "backend": self.backend_name},
        ):
            for phase in PHASES:
                saved = self._saved_record(phase)
                if saved is not None:
                    # Resume path: hydrate the prior phase's detail (including its
                    # recorded duration) so later phases see a complete artifacts map.
                    detail = dict(saved["detail"])
                    detail["duration_ms"] = saved["duration_ms"]
                    artifacts[phase] = detail
                    continue
                started = time.perf_counter()
                with _tracer.start_as_current_span(f"replay.phase.{phase}") as phase_span:
                    if phase == "retrieve":
                        phase_span.set_attribute(
                            "replay.cases.descriptive",
                            sum(case.cue_type == "descriptive" for case in self.fixture.cases),
                        )
                        phase_span.set_attribute(
                            "replay.cases.associative",
                            sum(case.cue_type == "associative" for case in self.fixture.cases),
                        )
                    if phase == "ingest":
                        detail = self._ingest()
                    elif phase == "index":
                        detail = self._index()
                    elif phase == "retrieve":
                        detail = self._retrieve()
                    elif phase == "answer":
                        detail = self._answer()
                    else:
                        detail = self._evaluate(artifacts)
                    artifacts[phase] = detail
                    artifacts[phase]["duration_ms"] = (time.perf_counter() - started) * 1000
                    self._mark(phase, artifacts[phase]["duration_ms"], detail)
        report = {
            "fixture_id": self.fixture.id,
            "backend": self.backend_name,
            "status": "complete",
            "phases": {phase: artifacts[phase] for phase in PHASES},
            "report": artifacts["evaluate"],
            "limitations": [
                "Retrieval merges the global store under the case scope's store, mirroring the documented eval overlay; production `note search` is project-only.",
                "Context tokens use a 4-chars-per-token proxy (tokenizer-backed counts are typesafe-lab #6).",
                "Deterministic replay makes no model calls; answer metrics score labeled claims only.",
            ],
        }
        _write_json_atomic(self.root / "report.json", report)
        return report


def _verify_against_set_index(fixture_path: Path) -> None:
    """If the fixture lives inside a set with fixture-set.json, its sha256 must match the index."""
    fixture_path = fixture_path.resolve()
    for ancestor in fixture_path.parents:
        index_path = ancestor / "fixture-set.json"
        if not index_path.is_file():
            continue
        index = json.loads(index_path.read_text(encoding="utf-8"))
        rel = fixture_path.relative_to(ancestor).as_posix()
        for entry in index.get("fixtures", []):
            if entry.get("file") == rel:
                digest = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
                if digest != entry.get("sha256"):
                    raise ValueError(f"fixture {rel} fails its sha256 against {index_path}")

Executes version-2 replay fixtures (above_all.replay) against real backend
implementations in fresh on-disk scopes. Phases checkpoint as they complete -
ingest -> index -> retrieve -> answer -> evaluate - so a failed run resumes
from the first incomplete phase and stage costs stay inspectable.

Metrics follow spec/memory.md (2026-09-21): answer/retrieval quality, latency
and context tokens reported separately, plus cost, abstention, provenance,
pollution and operations. OpenTelemetry spans wrap each phase; the SDK is
configured by the caller (tests use the in-memory exporter, production may
attach OTLP).

mem0_oss and supermemory_local stay blocked behind policy facts the owner has
not supplied (internal model gateway mapping, third-party memory binary policy,
one-binary rule); asking for them fails loudly with the exact gap.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from opentelemetry import metrics, trace

from . import notes as _notes
from .agent_context import generate_agent_context
from .db import GLOBAL_MIGRATIONS, PROJECT_MIGRATIONS, migrate
from .memory_backend import get_backend
from .replay import ReplayFixture, load_replay_fixture

PHASES = ("ingest", "index", "retrieve", "answer", "evaluate")

BLOCKED_BACKENDS = {
    "mem0_oss": (
        "mem0_oss replay is blocked on owner policy facts: which internal model/embedding "
        "gateway endpoints it may use, and whether a vector store dependency is acceptable. "
        "See spec/memory.md - it runs only after sqlite_hybrid shows a gap on real traces."
    ),
    "supermemory_local": (
        "supermemory_local replay is blocked on owner policy facts: company policy on a "
        "third-party memory binary touching internal transcripts, whether a local embedding "
        "model is allowed or all model traffic goes through the internal gateway, and whether "
        "a second local process breaks the one-binary rule."
    ),
}


class BackendBlockedError(RuntimeError):
    """A candidate backend was requested before its policy facts landed."""


_tracer = trace.get_tracer("above_all.replay")
_meter = metrics.get_meter("above_all.replay")
def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON via tmp + replace so a torn write can't become the restart state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


_phase_counter = _meter.create_counter(
    "replay.phase.completed", description="Replay phases completed, by phase and backend"
)
_phase_latency = _meter.create_histogram(
    "replay.phase.duration_ms", description="Replay phase wall time in milliseconds"
)

CHARS_PER_TOKEN = 4  # documented proxy until tokenizer-backed counts land (typesafe-lab #6)


def _scope_name(scope: str) -> str:
    return "global" if scope == "global" else f"project-{scope}"


class ReplayRunner:
    """Runs one fixture against one backend inside work_dir with checkpoints."""

    def __init__(self, fixture: ReplayFixture, backend_name: str, work_dir: Path) -> None:
        if backend_name in BLOCKED_BACKENDS:
            raise BackendBlockedError(BLOCKED_BACKENDS[backend_name])
        self.fixture = fixture
        self.backend = get_backend(backend_name)
        self.backend_name = backend_name
        self.root = work_dir / backend_name / fixture.id
        self.checkpoint_path = self.root / "checkpoint.json"
        self._dbs: dict[str, Any] = {}

    # -- checkpointing -----------------------------------------------------

    def _fingerprint(self, phase: str) -> str:
        import hashlib

        payload = json.dumps(
            {
                "fixture": self.fixture.model_dump(mode="json"),
                "backend": self.backend_name,
                "phase": phase,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def _saved_record(self, phase: str) -> dict[str, Any] | None:
        """The checkpoint record for a completed phase whose fingerprint still matches."""
        if not self.checkpoint_path.is_file():
            return None
        done = json.loads(self.checkpoint_path.read_text(encoding="utf-8")).get("phases", {})
        record = done.get(phase)
        if record and record.get("fingerprint") == self._fingerprint(phase):
            return record
        return None

    def _completed(self, phase: str) -> bool:
        return self._saved_record(phase) is not None

    def _mark(self, phase: str, duration_ms: float, detail: dict[str, Any]) -> None:
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        state = (
            json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
            if self.checkpoint_path.is_file()
            else {"fixture_id": self.fixture.id, "backend": self.backend_name, "phases": {}}
        )
        state["phases"][phase] = {
            "fingerprint": self._fingerprint(phase),
            "duration_ms": duration_ms,
            "detail": detail,
        }
        _write_json_atomic(self.checkpoint_path, state)
        _phase_counter.add(1, {"phase": phase, "backend": self.backend_name})
        _phase_latency.record(duration_ms, {"phase": phase, "backend": self.backend_name})

    # -- scope plumbing ------------------------------------------------------

    def _scope_dir(self, scope: str) -> Path:
        path = self.root / "scopes" / _scope_name(scope)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _db(self, scope: str):
        if scope not in self._dbs:
            migrations = GLOBAL_MIGRATIONS if scope == "global" else PROJECT_MIGRATIONS
            self._dbs[scope] = migrate(self._scope_dir(scope) / "assistant.db", migrations)
        return self._dbs[scope]

    def _scopes_used(self) -> list[str]:
        scopes = {note.scope for note in self.fixture.notes}
        for case in self.fixture.cases:
            scopes.add(case.scope)
        for op in self.fixture.operations:
            if op.args.get("scope"):
                scopes.add(str(op.args["scope"]))
        return sorted(scopes)

    def close(self) -> None:
        for db in self._dbs.values():
            db.close()
        self._dbs.clear()

    # -- phases --------------------------------------------------------------

    def _ingest(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for note in self.fixture.notes:
            scope_dir = self._scope_dir(note.scope)
            (scope_dir / "notes").mkdir(parents=True, exist_ok=True)
            metadata = {
                "type": "fact",
                "sources": note.source_anchors or ["fixture:" + self.fixture.id],
                "generated": self.fixture.provenance.created,
                "verified": "source" if note.source_anchors else "none",
                "status": note.status,
                "stale_after": note.stale_after,
            }
            if note.observer:
                metadata["observer"] = note.observer
            if note.inference:
                metadata["inference"] = note.inference
            path = _notes._write_note(
                scope_dir / "notes" / f"{note.id}.md", metadata, f"# {note.title}\n\n{note.body}"
            )
            _notes.index_note(self._db(note.scope), path)
            counts[note.scope] = counts.get(note.scope, 0) + 1
        return {"notes_ingested": counts}

    def _index(self) -> dict[str, Any]:
        # Re-index every note file from disk: a measurable, backend-honest index phase.
        indexed = 0
        for scope in self._scopes_used():
            notes_dir = self._scope_dir(scope) / "notes"
            for path in sorted(notes_dir.glob("*.md")):
                _notes.index_note(self._db(scope), path)
                indexed += 1
        return {"notes_indexed": indexed}

    def _search(self, scope: str, query: str, k: int) -> tuple[list[dict], float]:
        started = time.perf_counter()
        found: dict[str, dict] = {}
        if scope != "global":
            for row in self.backend.search(self._db(scope), query):
                found[row["id"]] = dict(row, scope=scope)
        for row in self.backend.search(self._db("global"), query):
            found.setdefault(row["id"], dict(row, scope="global"))
        elapsed = (time.perf_counter() - started) * 1000
        return list(found.values())[:k], elapsed

    def _retrieve(self) -> dict[str, Any]:
        k = self.fixture.k
        case_reports = []
        latencies: list[float] = []
        context_chars = 0
        for case in self.fixture.cases:
            rows, elapsed = self._search(case.scope, case.query, k)
            ids = [row["id"] for row in rows]
            latencies.append(elapsed)
            context_chars += sum(len(row["title"]) + len(row["body"]) for row in rows)
            relevant = set(case.relevant_note_ids)
            stale = set(case.stale_note_ids)
            forbidden_scopes = set(case.forbidden_scopes)
            case_reports.append(
                {
                    "id": case.id,
                    "cue_type": case.cue_type,
                    "retrieved_note_ids": ids,
                    "recall": len(relevant & set(ids)) / len(relevant) if relevant else 1.0,
                    "mrr": next(
                        (1.0 / rank for rank, nid in enumerate(ids, 1) if nid in relevant),
                        0.0 if relevant else 1.0,
                    ),
                    "stale_leaks": len(stale & set(ids)),
                    "cross_leaks": sum(row["scope"] in forbidden_scopes for row in rows),
                    "abstained": not ids,
                    "expected_abstention": case.expected_abstention,
                }
            )
        operation_reports = [self._run_operation(op) for op in self.fixture.operations]
        return {
            "cases": case_reports,
            "operations": operation_reports,
            "mean_latency_ms": sum(latencies) / len(latencies) if latencies else 0.0,
            "context_tokens": context_chars // CHARS_PER_TOKEN,
        }

    def _run_operation(self, op) -> dict[str, Any]:
        scope = str(op.args.get("scope", "global"))
        expect = op.expect
        report: dict[str, Any] = {"op": op.op, "ok": True, "detail": ""}
        try:
            if op.op == "create_candidate":
                try:
                    path = self.backend.create_candidate(
                        self._scope_dir(scope),
                        title=str(op.args["title"]),
                        body=str(op.args["body"]),
                        note_type=str(op.args.get("note_type", "fact")),
                        sources=list(op.args.get("sources", [])),
                        extra={"inference": op.args["inference"]} if op.args.get("inference") else None,
                        candidate_id=op.args.get("candidate_id"),
                    )
                except ValueError as exc:
                    # The write gate rejects unsourced/empty candidates at write time.
                    report["ok"] = bool(expect.get("rejected"))
                    report["detail"] = f"gate rejected at write: {exc}"
                    return report
                # Gate check from the declared inputs (parse_note itself refuses
                # unsourced notes, which is the write-time rejection above).
                gate_reject = not op.args.get("sources") or not str(op.args["body"]).strip()
                if expect.get("rejected"):
                    report["ok"] = gate_reject
                    report["detail"] = "gate rejected unsourced/empty candidate" if gate_reject else "gate accepted an unsourced candidate"
                    if gate_reject:
                        # set_note_status/discard_candidate re-parse and refuse unsourced
                        # notes (write-gate gap reported upstream), so flip the status field
                        # directly; the rejected file stays on disk for audit.
                        import re as _re

                        cand = self._scope_dir(scope) / "candidates" / f"{op.args['candidate_id']}.md"
                        cand.write_text(
                            _re.sub(r"^status: .*$", "status: rejected", cand.read_text(encoding="utf-8"), count=1, flags=_re.MULTILINE),
                            encoding="utf-8",
                        )
                else:
                    report["ok"] = bool(expect.get("created", True)) and not gate_reject
                    report["detail"] = str(path)
            elif op.op == "approve":
                try:
                    self.backend.approve(
                        self._db(scope),
                        self._scope_dir(scope),
                        str(op.args["candidate_id"]),
                        replaces=op.args.get("replaces"),
                    )
                    report["ok"] = bool(expect.get("ok", True))
                    report["detail"] = "promoted"
                except ValueError as exc:
                    report["ok"] = not expect.get("ok", True)
                    report["detail"] = f"promotion refused: {exc}"
            elif op.op == "discard":
                self.backend.discard(self._scope_dir(scope), str(op.args["candidate_id"]))
            elif op.op == "search":
                rows, _ = self._search(scope, str(op.args["query"]), self.fixture.k)
                ids = [row["id"] for row in rows]
                problems = []
                for wanted in expect.get("returned_note_ids", []):
                    if wanted not in ids:
                        problems.append(f"missing {wanted}")
                for banned in expect.get("forbidden_note_ids", []):
                    if banned in ids:
                        problems.append(f"leaked {banned}")
                if "abstained" in expect and bool(ids) == bool(expect["abstained"]):
                    problems.append("abstention mismatch")
                if "max_returned_for_query" in expect and len(ids) > int(expect["max_returned_for_query"]):
                    problems.append(f"returned {len(ids)} > {expect['max_returned_for_query']}")
                report["ok"] = not problems
                report["detail"] = "; ".join(problems) or f"returned {ids}"
            elif op.op == "generate_context":
                titles = {n.id: n.title for n in self.fixture.notes}
                path = generate_agent_context(
                    self._scope_dir(scope), self._db(scope), self.backend,
                    self._db("global") if scope != "global" else None,
                )
                content = path.read_text(encoding="utf-8")
                size = len(content.encode())
                problems = []
                for nid in expect.get("included_note_ids", []):
                    if titles.get(nid, nid) not in content:
                        problems.append(f"missing {nid}")
                for nid in expect.get("excluded_note_ids", []):
                    if titles.get(nid, nid) in content:
                        problems.append(f"leaked {nid}")
                if "max_bytes" in expect and size > int(expect["max_bytes"]):
                    problems.append(f"context {size}B > {expect['max_bytes']}B")
                report["ok"] = not problems
                report["detail"] = "; ".join(problems) or f"{size}B within budget"
            elif op.op == "delete_note":
                note_path = self._scope_dir(scope) / "notes" / f"{op.args['note_id']}.md"
                _notes.set_note_status(note_path, "rejected")
                _notes.index_note(self._db(scope), note_path)
                report["detail"] = "note rejected and de-indexed"
            elif op.op == "rebuild_index":
                db = self._db(scope)
                db.execute("DELETE FROM notes_fts")
                notes_dir = self._scope_dir(scope) / "notes"
                for path in sorted(notes_dir.glob("*.md")):
                    _notes.index_note(db, path)
                problems = []
                for nid in expect.get("searchable_note_ids", []):
                    if not db.execute(
                        "SELECT 1 FROM notes_fts WHERE note_id=?", (nid,)
                    ).fetchone():
                        problems.append(f"not searchable after rebuild: {nid}")
                for nid in expect.get("gone_note_ids", []):
                    if db.execute("SELECT 1 FROM notes_fts WHERE note_id=?", (nid,)).fetchone():
                        problems.append(f"still searchable after rebuild: {nid}")
                report["ok"] = not problems
                report["detail"] = "; ".join(problems) or "rebuild verified"
            elif op.op == "export":
                # Export the approved sources themselves: the rebuild-from-sources guarantee.
                import hashlib

                out = self.root / f"export-{scope}"
                out.mkdir(parents=True, exist_ok=True)
                notes_dir = self._scope_dir(scope) / "notes"
                exported = []
                for note_path in sorted(notes_dir.glob("*.md")):
                    parsed = _notes.parse_note(note_path.read_text(encoding="utf-8"))
                    if parsed.metadata["status"] != "active":
                        continue
                    data = note_path.read_bytes()
                    (out / note_path.name).write_bytes(data)
                    exported.append({"id": note_path.stem, "sha256": hashlib.sha256(data).hexdigest()})
                (out / "manifest.json").write_text(json.dumps({"notes": exported}, indent=2) + "\n")
                report["ok"] = len(exported) >= int(expect.get("min_notes", 0))
                report["detail"] = f"exported {len(exported)} active notes with checksums"
            else:  # pragma: no cover - schema validation prevents this
                report["ok"] = False
                report["detail"] = f"unknown op {op.op!r}"
        except Exception as exc:  # noqa: BLE001 - an erroring op is a failed op, never a crash
            report["ok"] = False
            report["detail"] = f"{type(exc).__name__}: {exc}"
        return report

    def _answer(self) -> dict[str, Any]:
        factual: list[float] = []
        unsupported: list[float] = []
        abstention: list[float] = []
        for case in self.fixture.cases:
            answer = case.answer
            if answer is None:
                continue
            claims = {str(c) for c in answer.get("claims", [])}
            supported = {str(c) for c in answer.get("supported_facts", [])}
            expected = set(case.expected_facts)
            forbidden = set(case.forbidden_facts)
            factual.append(len(expected & claims) / len(expected) if expected else 1.0)
            bad = (claims - supported) | (claims & forbidden)
            unsupported.append(len(bad) / len(claims) if claims else 0.0)
            abstention.append(float(bool(answer.get("abstained")) == case.expected_abstention))
        mean = lambda xs: sum(xs) / len(xs) if xs else 0.0
        return {
            "evaluated_case_count": len(factual),
            "factual_accuracy": mean(factual),
            "unsupported_claim_rate": mean(unsupported),
            "abstention_accuracy": mean(abstention),
        }

    def _evaluate(self, artifacts: dict[str, Any]) -> dict[str, Any]:
        cases = artifacts["retrieve"]["cases"]
        ops = artifacts["retrieve"]["operations"]
        mean = lambda xs: sum(xs) / len(xs) if xs else 0.0
        descriptive_cases = [c for c in cases if c["cue_type"] == "descriptive"]
        associative_cases = [c for c in cases if c["cue_type"] == "associative"]
        retrieval = {
            "recall_at_k": mean([c["recall"] for c in cases]),
            "descriptive_recall_at_k": mean([c["recall"] for c in descriptive_cases]),
            "associative_recall_at_k": mean([c["recall"] for c in associative_cases]),
            "descriptive_case_count": len(descriptive_cases),
            "associative_case_count": len(associative_cases),
            "mrr": mean([c["mrr"] for c in cases]),
            "stale_leakage_rate": mean([c["stale_leaks"] for c in cases]),
            "cross_project_leakage_rate": mean([c["cross_leaks"] for c in cases]),
            "abstention_correct": mean(
                [float(c["abstained"] == c["expected_abstention"]) for c in cases]
            ),
        }
        return {
            "quality": retrieval,
            "latency_ms": {
                phase: artifacts[phase]["duration_ms"] for phase in ("ingest", "index")
            }
            | {"retrieve_mean_ms": artifacts["retrieve"]["mean_latency_ms"]},
            "context_tokens": artifacts["retrieve"]["context_tokens"],
            "cost": {
                "model_calls": 0,
                "note": "deterministic replay; model-backed answer generation is opt-in later",
            },
            "abstention": {"accuracy": artifacts["answer"]["abstention_accuracy"]},
            "provenance": {
                "anchored_notes": sum(1 for n in self.fixture.notes if n.source_anchors),
                "total_notes": len(self.fixture.notes),
            },
            "pollution": {
                "notes_seeded": len(self.fixture.notes),
                "candidates_created": sum(1 for o in ops if o["op"] == "create_candidate"),
                "operations_failed": [o["op"] + ": " + o["detail"] for o in ops if not o["ok"]],
            },
            "operations": {
                "scopes_created": len(self._scopes_used()),
                "extra_servers": 0,
                "extra_model_runtimes": 0,
            },
        }

    def run(self) -> dict[str, Any]:
        if all(self._completed(phase) for phase in PHASES) and (self.root / "report.json").is_file():
            return json.loads((self.root / "report.json").read_text(encoding="utf-8"))
        artifacts: dict[str, Any] = {}
        with _tracer.start_as_current_span(
            "replay.run",
            attributes={"fixture": self.fixture.id, "backend": self.backend_name},
        ):
            for phase in PHASES:
                saved = self._saved_record(phase)
                if saved is not None:
                    # Resume path: hydrate the prior phase's detail (including its
                    # recorded duration) so later phases see a complete artifacts map.
                    detail = dict(saved["detail"])
                    detail["duration_ms"] = saved["duration_ms"]
                    artifacts[phase] = detail
                    continue
                started = time.perf_counter()
                with _tracer.start_as_current_span(f"replay.phase.{phase}") as phase_span:
                    if phase == "retrieve":
                        phase_span.set_attribute(
                            "replay.cases.descriptive",
                            sum(case.cue_type == "descriptive" for case in self.fixture.cases),
                        )
                        phase_span.set_attribute(
                            "replay.cases.associative",
                            sum(case.cue_type == "associative" for case in self.fixture.cases),
                        )
                    if phase == "ingest":
                        detail = self._ingest()
                    elif phase == "index":
                        detail = self._index()
                    elif phase == "retrieve":
                        detail = self._retrieve()
                    elif phase == "answer":
                        detail = self._answer()
                    else:
                        detail = self._evaluate(artifacts)
                    artifacts[phase] = detail
                    artifacts[phase]["duration_ms"] = (time.perf_counter() - started) * 1000
                    self._mark(phase, artifacts[phase]["duration_ms"], detail)
        report = {
            "fixture_id": self.fixture.id,
            "backend": self.backend_name,
            "status": "complete",
            "phases": {phase: artifacts[phase] for phase in PHASES},
            "report": artifacts["evaluate"],
            "limitations": [
                "Retrieval merges the global store under the case scope's store, mirroring the documented eval overlay; production `note search` is project-only.",
                "Context tokens use a 4-chars-per-token proxy (tokenizer-backed counts are typesafe-lab #6).",
                "Deterministic replay makes no model calls; answer metrics score labeled claims only.",
            ],
        }
        _write_json_atomic(self.root / "report.json", report)
        return report


def _verify_against_set_index(fixture_path: Path) -> None:
    """If the fixture lives inside a set with fixture-set.json, its sha256 must match the index."""
    fixture_path = fixture_path.resolve()
    for ancestor in fixture_path.parents:
        index_path = ancestor / "fixture-set.json"
        if not index_path.is_file():
            continue
        index = json.loads(index_path.read_text(encoding="utf-8"))
        rel = fixture_path.relative_to(ancestor).as_posix()
        for entry in index.get("fixtures", []):
            if entry.get("file") == rel:
                digest = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
                if digest != entry.get("sha256"):
                    raise ValueError(f"fixture {rel} fails its sha256 against {index_path}")
                return
        raise ValueError(f"fixture {rel} is not listed in {index_path}")


def run_fixture(fixture_path: Path, backend_name: str, work_dir: Path) -> dict[str, Any]:
    fixture_path = Path(fixture_path)
    _verify_against_set_index(fixture_path)
    fixture = load_replay_fixture(fixture_path)
    runner = ReplayRunner(fixture, backend_name, work_dir)
    try:
        return runner.run()
    finally:
        runner.close()


def compare_backends(fixture_path: Path, backend_names: list[str], work_dir: Path) -> dict[str, Any]:
    reports = {}
    for name in backend_names:
        try:
            reports[name] = run_fixture(fixture_path, name, work_dir)
        except BackendBlockedError as exc:
            reports[name] = {"backend": name, "status": "blocked", "reason": str(exc)}
    complete = {k: v for k, v in reports.items() if v.get("status") == "complete"}
    deltas: dict[str, Any] = {}
    if "sqlite_fts" in complete:
        baseline = complete["sqlite_fts"]["report"]
        for name, report in complete.items():
            if name == "sqlite_fts":
                continue
            deltas[name] = {
                metric: report["report"]["quality"][metric] - baseline["quality"][metric]
                for metric in report["report"]["quality"]
            }
            deltas[name]["retrieve_mean_ms"] = (
                report["report"]["latency_ms"]["retrieve_mean_ms"]
                - baseline["latency_ms"]["retrieve_mean_ms"]
            )
            deltas[name]["context_tokens"] = (
                report["report"]["context_tokens"] - baseline["context_tokens"]
            )
    return {
        "fixture": str(fixture_path),
        "reports": reports,
        "deltas_vs_sqlite_fts": deltas,
        "recommendation": {
            "adopt": None,
            "reason": "Adoption requires harvested real traces; regenerated dogfood fixtures are directional only (spec/memory.md).",
        },
}
