import json
from pathlib import Path

from above_all.extraction import extract_trace_candidate, usefulness_score

FIXTURES = Path(__file__).parent / "fixtures"


def _trace(tmp_path, name, messages):
    path = tmp_path / f"{name}.jsonl"
    rows = [
        {"type": "assistant", "sessionId": name, "uuid": f"a{i}", "message": {"content": text}}
        for i, text in enumerate(messages)
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


def test_real_dogfood_trace_extracts_health_fact_not_wrapper_summary():
    result = extract_trace_candidate(FIXTURES / "health-session.jsonl", "health-dogfood")
    assert (
        result.body
        == "`./health.sh` is the repository health command. It runs pytest and Ruff, and the file should remain executable."
    )
    assert result.evidence == ["trace-message:3"]
    assert "interactive session" not in result.body


def test_deterministic_fallback_abstains_on_process_boilerplate(tmp_path):
    path = _trace(
        tmp_path,
        "boiler",
        ["interactive session - exit 0 after 1.2s; context preloaded from /tmp/context."],
    )
    result = extract_trace_candidate(path, "boiler")
    assert result.body is None
    assert result.reason


def test_model_route_is_review_only_text_and_failure_falls_back(tmp_path):
    path = _trace(tmp_path, "route", ["`make test` is the test command."])
    assert (
        extract_trace_candidate(path, "route", lambda trace: "Model-proposed fact").method
        == "routed-model"
    )
    assert (
        extract_trace_candidate(
            path, "route", lambda trace: (_ for _ in ()).throw(RuntimeError())
        ).body
        == "`make test` is the test command."
    )


def test_candidate_usefulness_golden_set_before_after(tmp_path):
    cases = json.loads((FIXTURES / "candidate-usefulness.json").read_text())
    scores = []
    for case in cases:
        path = (
            FIXTURES / case["trace"]
            if "trace" in case
            else _trace(tmp_path, case["name"], case["messages"])
        )
        extracted = extract_trace_candidate(path, case["name"])
        scores.append(usefulness_score(extracted.body or "", case["expected_terms"]))
    assert scores == [1.0, 1.0, 1.0]
    assert sum(scores) / len(scores) == 1.0


def test_broad_copula_does_not_create_a_candidate(tmp_path):
    path = _trace(tmp_path, "copula", ["The run is complete and the results are available."])
    result = extract_trace_candidate(path, "copula")
    assert result.body is None


def test_only_final_assistant_answer_is_considered(tmp_path):
    path = _trace(tmp_path, "final-only", ["`make test` is the test command.", "Done."])
    result = extract_trace_candidate(path, "final-only")
    assert result.body is None
