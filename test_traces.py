import json
import sqlite3
from pathlib import Path

import pytest

from above_all.analysis import analyze
from above_all.traces import import_claude_code_jsonl


def write_jsonl(path: Path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def stock_rows():
    return [
        {"type": "user", "uuid": "u1", "sessionId": "s1", "timestamp": "2026-01-01T00:00:00Z", "message": {"role": "user", "content": "fix it"}},
        {"type": "assistant", "uuid": "a1", "sessionId": "s1", "timestamp": "2026-01-01T00:00:01Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "checking"}, {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "false"}}], "usage": {"input_tokens": 11, "output_tokens": 4, "cache_read_input_tokens": 2}}},
        {"type": "user", "uuid": "u2", "sessionId": "s1", "timestamp": "2026-01-01T00:00:02Z", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "exit 1"}]}},
        {"type": "assistant", "uuid": "branch", "sessionId": "s1", "isSidechain": True, "message": {"role": "assistant", "content": "superseded"}},
        {"type": "summary", "uuid": "compact", "sessionId": "s1", "summary": "compacted earlier turns", "leafUuid": "branch"},
        {"type": "progress", "uuid": "sub", "sessionId": "s1", "data": {"type": "agent_progress", "prompt": "child"}},
    ]


def test_imports_stock_messages_tools_usage_and_markers(tmp_path):
    path = tmp_path / "transcript.jsonl"; write_jsonl(path, stock_rows())
    db = sqlite3.connect(":memory:")
    result = import_claude_code_jsonl(db, path)
    assert result["messages"] == 4 and result["tool_calls"] == 1
    assert db.execute("SELECT status,error_class FROM trace_tool_calls").fetchone() == ("failed", "exit 1")
    assert db.execute("SELECT tokens_in,tokens_out,cached_tokens FROM trace_usage").fetchone() == (11, 4, 2)
    assert db.execute("SELECT active_branch FROM trace_messages WHERE source_id='branch'").fetchone()[0] == 0


def test_same_fingerprint_is_idempotent(tmp_path):
    path = tmp_path / "transcript.jsonl"; write_jsonl(path, stock_rows()); db = sqlite3.connect(":memory:")
    assert import_claude_code_jsonl(db, path)["status"] == "imported"
    assert import_claude_code_jsonl(db, path)["status"] == "noop"


def test_changed_fingerprint_refuses_overwrite(tmp_path):
    path = tmp_path / "transcript.jsonl"; write_jsonl(path, stock_rows()); db = sqlite3.connect(":memory:")
    import_claude_code_jsonl(db, path); path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="fingerprint changed"):
        import_claude_code_jsonl(db, path)


def test_parse_error_is_recorded_and_loud(tmp_path):
    path = tmp_path / "bad.jsonl"; path.write_text("{bad}\n"); db = sqlite3.connect(":memory:")
    with pytest.raises(ValueError, match="line 1"):
        import_claude_code_jsonl(db, path, "s1")
    assert db.execute("SELECT status FROM trace_imports").fetchone()[0] == "failed"


def test_unknown_record_is_warning_not_guess(tmp_path):
    rows = stock_rows() + [{"type": "future-thing", "uuid": "x", "sessionId": "s1"}]
    path = tmp_path / "transcript.jsonl"; write_jsonl(path, rows); db = sqlite3.connect(":memory:")
    assert "unsupported record" in import_claude_code_jsonl(db, path)["warnings"][0]


def test_analysis_is_quiet_without_enough_completed_outcomes(tmp_path):
    db = sqlite3.connect(":memory:"); db.execute("CREATE TABLE outcomes (id TEXT, status TEXT)")
    result = analyze(db, tmp_path, min_completed=1)
    assert result["status"] == "quiet" and not any(result["queues"].values())
