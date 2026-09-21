"""ZEE-69: replay fixture schemas, corpus integrity, and retrieval scoring."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from above_all.evals import evaluate_fixture
from above_all.replay import (
    FIXTURE_FAMILIES,
    CorpusManifest,
    ReplayFixture,
    build_fixture_set_index,
    load_corpus_manifest,
    load_replay_fixture,
    to_eval_fixture_v1,
    validate_fixture_set,
)

REPLAY_ROOT = Path(__file__).parent / "fixtures" / "replay"

RETRIEVAL_FIXTURES = [
    "golden-queries",
    "changed-facts",
    "cross-project-leakage",
    "staleness",
    "perspective-leakage",
    "source-fidelity",
]

OPERATION_FIXTURES = [
    "duplication",
    "unsupported-inference",
    "derived-claim-quarantine",
    "profile-freshness",
    "scope-isolation",
    "export-delete-rebuild",
]


def test_fixture_set_validates_clean() -> None:
    index = validate_fixture_set(REPLAY_ROOT)
    assert {entry.family for entry in index.fixtures} == set(FIXTURE_FAMILIES)


def test_corpus_manifest_anchors_real_files() -> None:
    corpus = load_corpus_manifest(REPLAY_ROOT / "traces" / "manifest.json")
    assert isinstance(corpus, CorpusManifest)
    assert len(corpus.sessions) == 7
    projects = {session.project for session in corpus.sessions}
    assert {"aurora", "borealis", None} == projects


def test_corpus_tamper_fails_validation(tmp_path: Path) -> None:
    target = REPLAY_ROOT / "traces" / "session-0001.jsonl"
    original = target.read_bytes()
    try:
        target.write_bytes(original + b" ")
        with pytest.raises(ValueError, match="sha256"):
            validate_fixture_set(REPLAY_ROOT)
    finally:
        target.write_bytes(original)
    validate_fixture_set(REPLAY_ROOT)


@pytest.mark.parametrize("fixture_id", RETRIEVAL_FIXTURES)
def test_retrieval_fixtures_score_perfect_recall_without_leakage(fixture_id: str) -> None:
    fixture = load_replay_fixture(REPLAY_ROOT / "fixtures" / f"{fixture_id}.json")
    report = evaluate_fixture(to_eval_fixture_v1(fixture))
    retrieval = report["retrieval"]
    assert retrieval["recall_at_k"] == pytest.approx(1.0)
    assert retrieval["mrr"] == pytest.approx(1.0)
    assert retrieval["stale_leakage_rate"] == 0.0
    assert retrieval["cross_project_leakage_rate"] == 0.0


@pytest.mark.parametrize("fixture_id", OPERATION_FIXTURES)
def test_operation_fixtures_are_structurally_valid(fixture_id: str) -> None:
    fixture = load_replay_fixture(REPLAY_ROOT / "fixtures" / f"{fixture_id}.json")
    assert fixture.operations, fixture_id
    with pytest.raises(ValueError, match="operation fixtures"):
        to_eval_fixture_v1(fixture)


def test_corpus_is_labeled_generated_dogfood_not_harvested() -> None:
    corpus = load_corpus_manifest(REPLAY_ROOT / "traces" / "manifest.json")
    assert corpus.origin == "generated-dogfood"


def test_corpus_anchored_fixtures_anchor_to_corpus_sessions() -> None:
    corpus = load_corpus_manifest(REPLAY_ROOT / "traces" / "manifest.json")
    known = {session.session_id for session in corpus.sessions}
    anchored = 0
    for path in (REPLAY_ROOT / "fixtures").glob("*.json"):
        fixture = load_replay_fixture(path)
        if fixture.provenance.trace_session_ids:
            anchored += 1
            assert fixture.provenance.origin in {"synthetic", "harvested", "mixed"}, path.name
            assert set(fixture.provenance.trace_session_ids) <= known, path.name
    assert anchored >= 5


def test_synthetic_set_cannot_be_marked_adoption_gate(tmp_path) -> None:
    import shutil

    root = tmp_path / "replay"
    shutil.copytree(REPLAY_ROOT, root)
    index_path = root / "fixture-set.json"
    index = json.loads(index_path.read_text())
    index["fixtures"][0]["adoption_gate"] = True
    index_path.write_text(json.dumps(index, indent=2) + "\n")
    with pytest.raises(ValueError, match="not gate-eligible"):
        validate_fixture_set(root)


def test_checked_in_set_has_no_adoption_gate_entries() -> None:
    index = validate_fixture_set(REPLAY_ROOT)
    assert not any(entry.adoption_gate for entry in index.fixtures)


def test_unknown_family_rejected() -> None:
    raw = json.loads((REPLAY_ROOT / "fixtures" / "staleness.json").read_text())
    raw["family"] = "made-up-family"
    with pytest.raises(ValueError, match="unknown fixture family"):
        ReplayFixture.model_validate(raw)


def test_harvested_origin_requires_trace_anchors() -> None:
    raw = json.loads((REPLAY_ROOT / "fixtures" / "staleness.json").read_text())
    raw["provenance"]["origin"] = "harvested"
    raw["provenance"]["trace_session_ids"] = []
    with pytest.raises(ValueError, match="source trace sessions"):
        ReplayFixture.model_validate(raw)


def test_case_referencing_unknown_note_rejected() -> None:
    raw = json.loads((REPLAY_ROOT / "fixtures" / "staleness.json").read_text())
    raw["cases"][0]["relevant_note_ids"] = ["ghost-note"]
    with pytest.raises(ValueError, match="unknown note"):
        ReplayFixture.model_validate(raw)


def test_index_rebuild_roundtrip() -> None:
    index = build_fixture_set_index(REPLAY_ROOT, name="replay-fixtures-v1", corpus="traces/manifest.json")
    assert len(index.fixtures) == len(FIXTURE_FAMILIES)
    validate_fixture_set(REPLAY_ROOT)


def test_empty_fixture_rejected() -> None:
    raw = json.loads((REPLAY_ROOT / "fixtures" / "staleness.json").read_text())
    raw["cases"] = []
    raw["operations"] = []
    with pytest.raises(ValueError, match="at least one retrieval case or operation"):
        ReplayFixture.model_validate(raw)


def test_associative_fixture_is_explicitly_synthetic_and_lexically_disjoint() -> None:
    fixture = load_replay_fixture(REPLAY_ROOT / "fixtures" / "associative-cues.json")
    assert fixture.provenance.origin == "synthetic"
    assert "synthetic" in fixture.provenance.generator.lower()
    assert fixture.provenance.trace_session_ids == []
    assert {case.cue_type for case in fixture.cases} == {"descriptive", "associative"}


def test_associative_case_with_lexical_leakage_is_rejected() -> None:
    raw = json.loads((REPLAY_ROOT / "fixtures" / "associative-cues.json").read_text())
    case = next(case for case in raw["cases"] if case["cue_type"] == "associative")
    case["query"] += " peanut"
    with pytest.raises(ValueError, match="lexical overlap"):
        ReplayFixture.model_validate(raw)
