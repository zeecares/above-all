"""Held-out, provider-neutral memory and answer-quality evaluation."""
from __future__ import annotations

import json
import math
import sqlite3
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class EvalNote:
    id: str
    scope: str
    title: str
    body: str
    status: str = "active"
    stale_after: str | None = None


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _interval(values: list[float]) -> list[float]:
    """Two-sided 95% t interval for descriptive paired deltas."""
    if not values:
        return [0.0, 0.0]
    mean = _mean(values)
    if len(values) == 1:
        return [mean, mean]
    t_critical_95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060, 26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042}.get(len(values) - 1, 1.96)
    margin = t_critical_95 * statistics.stdev(values) / math.sqrt(len(values))
    return [mean - margin, mean + margin]


def _fact_set(answer: dict[str, Any] | None, key: str) -> set[str]:
    if not answer:
        return set()
    value = answer.get(key, [])
    return {str(item) for item in value}


def _make_index(notes: list[EvalNote], scope: str) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE notes(id TEXT PRIMARY KEY, scope TEXT, title TEXT, body TEXT)")
    db.execute("CREATE VIRTUAL TABLE notes_fts USING fts5(note_id UNINDEXED,title,body,tokenize='porter unicode61')")
    today = datetime.now(timezone.utc).date().isoformat()
    selected: dict[str, EvalNote] = {}
    for note in notes:
        if note.scope not in {"global", scope} or note.status != "active":
            continue
        if note.stale_after is not None and note.stale_after < today:
            continue
        # Project rows shadow global rows with the same stable ID.
        if note.id not in selected or note.scope == scope:
            selected[note.id] = note
    for note in selected.values():
        db.execute("INSERT INTO notes VALUES(?,?,?,?)", (note.id, note.scope, note.title, note.body))
        db.execute("INSERT INTO notes_fts VALUES(?,?,?)", (note.id, note.title, note.body))
    return db


def _retrieve(db: sqlite3.Connection, query: str, k: int) -> list[dict[str, Any]]:
    started = time.perf_counter()
    rows = db.execute(
        "SELECT n.id,n.scope,n.title,n.body FROM notes_fts f JOIN notes n ON n.id=f.note_id "
        "WHERE notes_fts MATCH ? ORDER BY bm25(notes_fts) LIMIT ?", (query, k)
    ).fetchall()
    elapsed = (time.perf_counter() - started) * 1000
    return [{**dict(row), "latency_ms": elapsed} for row in rows]


def evaluate_fixture(fixture: dict[str, Any], *, k: int | None = None) -> dict[str, Any]:
    """Evaluate one labeled fixture without calling a model."""
    version = fixture.get("version")
    if version != 1:
        raise ValueError(f"unsupported eval fixture version {version!r}")
    top_k = int(k or fixture.get("k", 5))
    if top_k < 1:
        raise ValueError("k must be positive")
    notes = [EvalNote(**item) for item in fixture.get("notes", [])]
    cases = fixture.get("cases", [])
    if not cases:
        raise ValueError("fixture must contain at least one case")

    precision: list[float] = []
    recall: list[float] = []
    reciprocal_ranks: list[float] = []
    stale_leaks = cross_leaks = 0
    retrieved_total = 0
    factual_scores: list[float] = []
    unsupported_rates: list[float] = []
    abstention_scores: list[float] = []
    retrieval_latency: list[float] = []
    case_reports: list[dict[str, Any]] = []

    for case in cases:
        scope = str(case.get("scope", "global"))
        db = _make_index(notes, scope)
        try:
            found = _retrieve(db, str(case["query"]), top_k)
        finally:
            db.close()
        found_ids = [row["id"] for row in found]
        relevant = {str(item) for item in case.get("relevant_note_ids", [])}
        hits = relevant.intersection(found_ids)
        precision.append(len(hits) / top_k if found_ids else (1.0 if not relevant else 0.0))
        recall.append(len(hits) / len(relevant) if relevant else 1.0)
        rank = next((i for i, note_id in enumerate(found_ids, 1) if note_id in relevant), None)
        reciprocal_ranks.append(1.0 / rank if rank else (1.0 if not relevant else 0.0))
        stale = {str(item) for item in case.get("stale_note_ids", [])}
        forbidden_scopes = {str(item) for item in case.get("forbidden_scopes", [])}
        stale_leaks += len(stale.intersection(found_ids))
        cross_leaks += sum(row["scope"] in forbidden_scopes for row in found)
        retrieved_total += len(found)
        retrieval_latency.extend(row["latency_ms"] for row in found[:1])

        answer = case.get("answer")
        expected = {str(item) for item in case.get("expected_facts", [])}
        forbidden = {str(item) for item in case.get("forbidden_facts", [])}
        supported = _fact_set(answer, "supported_facts")
        claims = _fact_set(answer, "claims")
        expected_abstention = bool(case.get("expected_abstention", False))
        actual_abstention = bool(answer and answer.get("abstained", False))
        if answer is not None:
            factual_scores.append(len(expected.intersection(claims)) / len(expected) if expected else 1.0)
            unsupported = (claims - supported) | claims.intersection(forbidden)
            unsupported_rates.append(len(unsupported) / len(claims) if claims else 0.0)
            abstention_scores.append(float(actual_abstention == expected_abstention))
        case_reports.append({
            "id": str(case["id"]), "retrieved_note_ids": found_ids,
            "relevant_found": sorted(hits), "expected_abstention": expected_abstention,
        })

    runs = fixture.get("comparisons", [])
    deltas: dict[str, Any] = {"sample_count": len(runs)}
    for metric in ("input_tokens", "output_tokens", "cached_tokens", "cost_usd", "latency_ms"):
        values = [float(run["with_memory"][metric]) - float(run["no_memory"][metric]) for run in runs]
        deltas[metric] = {"mean_delta": _mean(values), "confidence_interval_95": _interval(values)}

    report = {
        "fixture_version": 1, "k": top_k, "case_count": len(cases),
        "retrieval": {
            "precision_at_k": _mean(precision), "recall_at_k": _mean(recall),
            "mrr": _mean(reciprocal_ranks), "stale_leakage_rate": stale_leaks / max(retrieved_total, 1),
            "cross_project_leakage_rate": cross_leaks / max(retrieved_total, 1),
            "mean_latency_ms": _mean(retrieval_latency),
        },
        "answers": {
            "evaluated_case_count": len(factual_scores),
            "factual_accuracy": _mean(factual_scores),
            "unsupported_claim_rate": _mean(unsupported_rates),
            "abstention_accuracy": _mean(abstention_scores),
        },
        "with_memory_vs_no_memory": deltas,
        "cases": case_reports,
        "limitations": [
            "Answer metrics score labeled claims supplied by the fixture; deterministic CI does not generate answers.",
            "Token, cost, and latency deltas are descriptive paired measurements, not proof of savings.",
            "This labeled recall is distinct from the operational event proxy named retrieval_recall.",
        ],
    }
    return report


def load_fixture(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def check_thresholds(report: dict[str, Any], thresholds: dict[str, Any]) -> list[str]:
    failures = []
    sections = {"retrieval": report["retrieval"], "answers": report["answers"]}
    for section, values in thresholds.items():
        if section not in sections:
            raise ValueError(f"unknown threshold section {section!r}")
        for metric, rule in values.items():
            if metric not in sections[section]:
                raise ValueError(f"unknown threshold metric {section}.{metric}")
            actual = float(sections[section][metric])
            bounds = {"min": rule} if isinstance(rule, (int, float)) else rule
            if not isinstance(bounds, dict) or not set(bounds).issubset({"min", "max"}):
                raise ValueError(f"invalid threshold rule for {section}.{metric}")
            if "min" in bounds and actual < float(bounds["min"]):
                failures.append(f"{section}.{metric}={actual:.6f} below {float(bounds['min']):.6f}")
            if "max" in bounds and actual > float(bounds["max"]):
                failures.append(f"{section}.{metric}={actual:.6f} above {float(bounds['max']):.6f}")
    return failures



def _retrieve_hybrid(db: sqlite3.Connection, query: str, k: int) -> list[dict[str, Any]]:
    from .hybrid import embed, reciprocal_rank_fusion
    started = time.perf_counter()
    lexical = _retrieve(db, query, max(k, 20))
    q = embed(query)
    semantic = []
    for row in db.execute("SELECT id,scope,title,body FROM notes"):
        score = sum(a * b for a, b in zip(q, embed(f"{row['title']}\n{row['body']}")))
        if score > 0:
            semantic.append((score, dict(row)))
    semantic.sort(key=lambda item: (-item[0], item[1]["id"]))
    fused = reciprocal_rank_fusion(lexical, [row for _, row in semantic], limit=k)
    elapsed = (time.perf_counter() - started) * 1000
    return [{**row, "latency_ms": elapsed} for row in fused]


def _evaluate_retriever(fixture: dict[str, Any], retriever, k: int) -> dict[str, float]:
    notes = [EvalNote(**item) for item in fixture.get("notes", [])]
    precision = []; recall = []; mrr = []; latencies = []
    for case in fixture["cases"]:
        db = _make_index(notes, str(case.get("scope", "global")))
        try:
            found = retriever(db, str(case["query"]), k)
        finally:
            db.close()
        ids = [row["id"] for row in found]
        relevant = set(map(str, case.get("relevant_note_ids", [])))
        hits = relevant.intersection(ids)
        precision.append(len(hits) / k if ids else (1.0 if not relevant else 0.0))
        recall.append(len(hits) / len(relevant) if relevant else 1.0)
        rank = next((i for i, item in enumerate(ids, 1) if item in relevant), None)
        mrr.append(1.0 / rank if rank else (1.0 if not relevant else 0.0))
        if found: latencies.append(found[0]["latency_ms"])
    return {"precision_at_k": _mean(precision), "recall_at_k": _mean(recall), "mrr": _mean(mrr), "mean_latency_ms": _mean(latencies)}


def compare_retrievers(fixture: dict[str, Any], *, k: int | None = None) -> dict[str, Any]:
    """Measure opt-in hybrid against the FTS baseline on the same held-out cases."""
    top_k = int(k or fixture.get("k", 5))
    baseline = _evaluate_retriever(fixture, _retrieve, top_k)
    candidate = _evaluate_retriever(fixture, _retrieve_hybrid, top_k)
    delta = {key: candidate[key] - baseline[key] for key in baseline}
    # The synthetic golden fixture is a safety gate, not evidence for a default flip.
    return {
        "baseline_backend": "sqlite_fts", "candidate_backend": "sqlite_hybrid", "k": top_k,
        "baseline": baseline, "candidate": candidate, "delta": delta,
        "recommendation": {
            "enable_by_default": False,
            "reason": "FTS remains default until a material gain is reproduced on a larger real held-out query set.",
        },
        "limitations": [
            "The checked-in golden set is tiny and mostly lexical; this comparison is directional, not production evidence.",
            "The local feature hash has no model download but is not a general semantic sentence embedding.",
        ],
    }
