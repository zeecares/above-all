"""Bounded, review-only memory extraction from explicit session evidence."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .traces import ParsedTrace, parse_claude_code_jsonl

MAX_CLAIM_CHARS = 1200
_BOILERPLATE = (
    "interactive session",
    "context preloaded from",
    "path(s) changed",
    "exit 0 after",
    "exit summary",
    "session completed",
)
_FACT_SIGNALS = re.compile(
    r"(?:`[^`]+`|(?:\./|/)[\w./-]+|\b(?:must|should|uses?|checks?|requires?|commands?)\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Extraction:
    body: str | None
    method: str
    evidence: list[str]
    reason: str | None = None


def _active_assistant_messages(trace: ParsedTrace) -> list[tuple[int, str, str]]:
    return [
        (int(row[2]), str(row[4] or ""), str(row[5]).strip())
        for row in trace.messages
        if row[3] == "assistant" and row[6] and str(row[5]).strip()
    ]


def deterministic_extract(trace: ParsedTrace) -> Extraction:
    """Extract only explicit final-answer claims; abstain rather than invent.

    This fallback cannot infer lessons from tool calls or intermediate reasoning.
    It preserves the final assistant text when that text contains a fact/procedure
    signal and rejects known wrapper/process boilerplate.
    """
    messages = _active_assistant_messages(trace)
    if messages:
        seq, _ts, text = messages[-1]
        lines = []
        for raw in text.splitlines():
            line = raw.strip().lstrip("-* ")
            if not line or any(marker in line.lower() for marker in _BOILERPLATE):
                continue
            if _FACT_SIGNALS.search(line):
                lines.append(line)
        body = "\n".join(lines).strip()
        if body:
            return Extraction(
                body[:MAX_CLAIM_CHARS], "deterministic-final-answer", [f"trace-message:{seq}"]
            )
    return Extraction(
        None, "deterministic-final-answer", [], "no explicit useful claim in final answers"
    )


def extract_trace_candidate(
    trace_path: Path,
    session_id: str,
    model_extractor: Callable[[ParsedTrace], str | None] | None = None,
) -> Extraction:
    """Use a routed model seam when supplied, otherwise the honest fallback.

    The caller owns provider-neutral routing and supplies a callable. Model output
    remains a candidate and must be source anchored; empty/error output falls back
    deterministically.
    """
    trace = parse_claude_code_jsonl(trace_path, session_id)
    if model_extractor is not None:
        try:
            body = (model_extractor(trace) or "").strip()
            if body:
                return Extraction(
                    body[:MAX_CLAIM_CHARS], "routed-model", ["trace:model-extraction"]
                )
        except (OSError, RuntimeError, ValueError):
            # Provider failure must not erase the deterministic path.
            model_failed = True
            del model_failed
    return deterministic_extract(trace)


def usefulness_score(body: str, expected_terms: list[str]) -> float:
    """Transparent acceptance metric: fraction of labeled terms present."""
    if not expected_terms:
        raise ValueError("expected_terms must not be empty")
    lower = body.lower()
    return sum(term.lower() in lower for term in expected_terms) / len(expected_terms)
