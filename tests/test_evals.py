import json
from pathlib import Path

from above_all.evals import check_thresholds, evaluate_fixture, load_fixture

FIXTURE = Path(__file__).parent / "fixtures" / "eval-golden.json"


def test_golden_eval_is_reproducible_and_catches_leakage():
    report = evaluate_fixture(load_fixture(FIXTURE))
    assert report["retrieval"]["recall_at_k"] == 1.0
    assert report["retrieval"]["stale_leakage_rate"] == 0.0
    assert report["retrieval"]["cross_project_leakage_rate"] == 0.0
    assert report["answers"]["factual_accuracy"] == 1.0
    assert report["answers"]["unsupported_claim_rate"] == 0.0
    assert report["answers"]["abstention_accuracy"] == 1.0
    assert report["with_memory_vs_no_memory"]["sample_count"] == 2


def test_thresholds_fail_a_known_regression():
    report = evaluate_fixture(load_fixture(FIXTURE))
    assert check_thresholds(report, {"retrieval": {"recall_at_k": 1.01}})
    assert not check_thresholds(report, {"retrieval": {"recall_at_k": 1.0}})


def test_rejects_unversioned_fixture():
    try:
        evaluate_fixture({"cases": [{"id": "x", "query": "x"}]})
    except ValueError as error:
        assert "version" in str(error)
    else:
        raise AssertionError("expected ValueError")
