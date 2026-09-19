import copy
from pathlib import Path

import pytest

from above_all.evals import _interval, check_thresholds, evaluate_fixture, load_fixture

FIXTURE = Path(__file__).parent / "fixtures" / "eval-golden.json"


def test_golden_eval_is_reproducible_and_catches_leakage():
    report = evaluate_fixture(load_fixture(FIXTURE))
    assert report["retrieval"]["precision_at_k"] == pytest.approx(2 / 3)
    assert report["retrieval"]["recall_at_k"] == 1.0
    assert report["retrieval"]["stale_leakage_rate"] == 0.0
    assert report["retrieval"]["cross_project_leakage_rate"] == 0.0
    assert report["answers"]["factual_accuracy"] == 1.0
    assert report["answers"]["unsupported_claim_rate"] == 0.0
    assert report["answers"]["abstention_accuracy"] == 1.0
    assert report["with_memory_vs_no_memory"]["sample_count"] == 2


def test_golden_cases_fail_when_stale_or_cross_project_notes_are_visible():
    fixture = load_fixture(FIXTURE)
    stale = copy.deepcopy(fixture)
    next(note for note in stale["notes"] if note["id"] == "old-budget").pop("stale_after")
    assert evaluate_fixture(stale)["retrieval"]["stale_leakage_rate"] > 0

    cross_project = copy.deepcopy(fixture)
    case = next(case for case in cross_project["cases"] if case["id"] == "project-isolation")
    case["forbidden_scopes"] = ["project-a"]
    assert evaluate_fixture(cross_project)["retrieval"]["cross_project_leakage_rate"] > 0


def test_answer_metrics_fail_wrong_and_unsupported_claims():
    fixture = load_fixture(FIXTURE)
    fixture["cases"][0]["answer"] = {
        "claims": ["color=red"],
        "supported_facts": ["color=red"],
        "abstained": False,
    }
    report = evaluate_fixture(fixture)
    assert report["answers"]["factual_accuracy"] < 1.0
    assert report["answers"]["unsupported_claim_rate"] > 0


def test_paired_interval_uses_small_sample_t_critical_value():
    low, high = _interval([-30.0, -25.0])
    assert low == pytest.approx(-59.265, abs=0.001)
    assert high == pytest.approx(4.265, abs=0.001)


def test_thresholds_gate_minimums_and_maximums():
    report = evaluate_fixture(load_fixture(FIXTURE))
    assert check_thresholds(report, {"retrieval": {"recall_at_k": 1.01}})
    assert not check_thresholds(report, {"retrieval": {"recall_at_k": 1.0}})
    assert check_thresholds(report, {"answers": {"unsupported_claim_rate": {"max": -0.01}}})
    assert not check_thresholds(report, {"answers": {"unsupported_claim_rate": {"max": 0.0}}})


def test_rejects_unversioned_fixture():
    with pytest.raises(ValueError, match="version"):
        evaluate_fixture({"cases": [{"id": "x", "query": "x"}]})
