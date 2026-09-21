"""ZEE-70: phase-checkpointed replay runner over real backends."""
from __future__ import annotations

from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from above_all.replay import load_replay_fixture
from above_all.replay_runner import (
    PHASES,
    BackendBlockedError,
    ReplayRunner,
    compare_backends,
    run_fixture,
)

REPLAY_ROOT = Path(__file__).parent / "fixtures" / "replay"
ALL_FIXTURES = sorted((REPLAY_ROOT / "fixtures").glob("*.json"))


_EXPORTER = InMemorySpanExporter()
_PROVIDER = TracerProvider()
_PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTER))
trace.set_tracer_provider(_PROVIDER)


@pytest.fixture(autouse=True)
def otel_in_memory():
    _EXPORTER.clear()
    yield _EXPORTER


RETRIEVAL_GATED = {"golden-queries", "changed-facts", "cross-project-leakage", "staleness", "perspective-leakage", "source-fidelity"}


@pytest.mark.parametrize("fixture_path", ALL_FIXTURES, ids=lambda p: p.stem)
def test_every_fixture_runs_clean_on_the_reference_backend(fixture_path, tmp_path, otel_in_memory):
    report = run_fixture(fixture_path, "sqlite_fts", tmp_path)
    assert report["status"] == "complete"
    result = report["report"]
    failed = result["pollution"]["operations_failed"]
    assert not failed, f"{fixture_path.stem}: {failed}"
    if fixture_path.stem in RETRIEVAL_GATED:
        assert result["quality"]["recall_at_k"] == pytest.approx(1.0)
        assert result["quality"]["stale_leakage_rate"] == 0
        assert result["quality"]["cross_project_leakage_rate"] == 0


@pytest.mark.parametrize("fixture_path", ALL_FIXTURES, ids=lambda p: p.stem)
def test_hybrid_runs_to_completion_and_reports_honestly(fixture_path, tmp_path, otel_in_memory):
    # sqlite_hybrid is the measured-worse opt-in candidate (ZEE-63). The runner
    # must complete and REPORT its gaps, not hide them: no leakage ever, but
    # weak semantic matches currently break abstention and dilute recall.
    report = run_fixture(fixture_path, "sqlite_hybrid", tmp_path)
    assert report["status"] == "complete"
    result = report["report"]
    assert result["quality"]["stale_leakage_rate"] == 0
    assert result["quality"]["cross_project_leakage_rate"] == 0


def test_hybrid_abstention_gap_is_visible_in_the_report(tmp_path):
    report = run_fixture(REPLAY_ROOT / "fixtures" / "export-delete-rebuild.json", "sqlite_hybrid", tmp_path)
    failed = report["report"]["pollution"]["operations_failed"]
    assert any("abstention mismatch" in item for item in failed), failed


def test_phases_checkpoint_and_resume(tmp_path):
    fixture = load_replay_fixture(REPLAY_ROOT / "fixtures" / "golden-queries.json")
    runner = ReplayRunner(fixture, "sqlite_fts", tmp_path)
    runner.run()
    runner.close()
    checkpoint = (runner.root / "checkpoint.json")
    assert checkpoint.is_file()
    import json

    state = json.loads(checkpoint.read_text())
    assert set(state["phases"]) == set(PHASES)
    # Second run: every phase fingerprint matches, nothing re-executes.
    runner2 = ReplayRunner(fixture, "sqlite_fts", tmp_path)
    try:
        for phase in PHASES:
            assert runner2._completed(phase), phase
        report = runner2.run()
    finally:
        runner2.close()
    assert report["status"] == "complete"


def test_partial_resume_after_interruption(tmp_path):
    import json

    fixture = load_replay_fixture(REPLAY_ROOT / "fixtures" / "golden-queries.json")
    runner = ReplayRunner(fixture, "sqlite_fts", tmp_path)
    runner.run()
    runner.close()

    # Simulate a crash after `index`: later checkpoints and the report are gone.
    checkpoint = runner.root / "checkpoint.json"
    state = json.loads(checkpoint.read_text())
    prior = {phase: state["phases"][phase] for phase in ("ingest", "index")}
    for phase in ("retrieve", "answer", "evaluate"):
        del state["phases"][phase]
    checkpoint.write_text(json.dumps(state, indent=2) + "\n")
    (runner.root / "report.json").unlink()

    # Fresh runner over the same workdir completes without re-executing prior phases.
    runner2 = ReplayRunner(fixture, "sqlite_fts", tmp_path)
    try:
        report = runner2.run()
    finally:
        runner2.close()
    assert report["status"] == "complete"
    resumed = json.loads(checkpoint.read_text())
    for phase, record in prior.items():
        assert resumed["phases"][phase] == record, f"{phase} was re-executed or rewritten"
    assert set(resumed["phases"]) == set(PHASES)
    # Hydrated prior-phase artifacts feed the evaluate phase, not a KeyError.
    assert report["report"]["latency_ms"]["ingest"] == prior["ingest"]["duration_ms"]
    assert report["report"]["latency_ms"]["index"] == prior["index"]["duration_ms"]
    assert report["phases"]["ingest"]["duration_ms"] == prior["ingest"]["duration_ms"]


def test_checkpoint_and_report_writes_are_atomic(tmp_path):
    fixture = load_replay_fixture(REPLAY_ROOT / "fixtures" / "golden-queries.json")
    runner = ReplayRunner(fixture, "sqlite_fts", tmp_path)
    try:
        runner.run()
    finally:
        runner.close()
    # No torn tmp files survive a clean run.
    assert not list(runner.root.glob("*.tmp"))
    assert not list((runner.root / "scopes").glob("**/*.tmp")) if (runner.root / "scopes").exists() else True


def test_blocked_backends_fail_loud(tmp_path):
    for name in ("mem0_oss", "supermemory_local"):
        with pytest.raises(BackendBlockedError, match="policy facts"):
            run_fixture(REPLAY_ROOT / "fixtures" / "staleness.json", name, tmp_path)


def test_compare_reports_deltas_and_blocked(tmp_path):
    out = compare_backends(
        REPLAY_ROOT / "fixtures" / "golden-queries.json",
        ["sqlite_fts", "sqlite_hybrid", "supermemory_local"],
        tmp_path,
    )
    assert out["reports"]["sqlite_fts"]["status"] == "complete"
    assert out["reports"]["sqlite_hybrid"]["status"] == "complete"
    assert out["reports"]["supermemory_local"]["status"] == "blocked"
    assert "sqlite_hybrid" in out["deltas_vs_sqlite_fts"]
    assert out["recommendation"]["adopt"] is None


def test_otel_spans_cover_all_phases(tmp_path, otel_in_memory):
    run_fixture(REPLAY_ROOT / "fixtures" / "staleness.json", "sqlite_fts", tmp_path)
    names = {span.name for span in otel_in_memory.get_finished_spans()}
    assert "replay.run" in names
    for phase in PHASES:
        assert f"replay.phase.{phase}" in names


def test_hybrid_latency_is_reported_not_hidden(tmp_path):
    report = run_fixture(REPLAY_ROOT / "fixtures" / "golden-queries.json", "sqlite_hybrid", tmp_path)
    latency = report["report"]["latency_ms"]
    assert latency["retrieve_mean_ms"] >= 0
    assert set(latency) == {"ingest", "index", "retrieve_mean_ms"}


def test_associative_and_descriptive_recall_are_scored_separately(tmp_path, otel_in_memory):
    report = run_fixture(REPLAY_ROOT / "fixtures" / "associative-cues.json", "sqlite_fts", tmp_path)
    quality = report["report"]["quality"]
    assert quality["descriptive_case_count"] == 3
    assert quality["associative_case_count"] == 3
    assert quality["descriptive_recall_at_k"] == pytest.approx(1.0)
    # Intended negative control: lexical FTS cannot bridge zero-overlap cues.
    assert quality["associative_recall_at_k"] == pytest.approx(0.0)
    retrieve_span = next(
        span for span in otel_in_memory.get_finished_spans() if span.name == "replay.phase.retrieve"
    )
    assert retrieve_span.attributes["replay.cases.descriptive"] == 3
    assert retrieve_span.attributes["replay.cases.associative"] == 3
