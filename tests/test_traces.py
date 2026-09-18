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
        {"type": "progress", "uuid": "sub", "sessionId": "s1", "parentUuid": "a1", "data": {"type": "agent_progress", "agentId": "child-1", "prompt": "child"}},
    ]


def test_imports_stock_messages_tools_usage_and_markers(tmp_path):
    path = tmp_path / "transcript.jsonl"; write_jsonl(path, stock_rows())
    db = sqlite3.connect(":memory:")
    result = import_claude_code_jsonl(db, path)
    assert result["messages"] == 4 and result["tool_calls"] == 1
    assert db.execute("SELECT status,error_class FROM trace_tool_calls").fetchone() == ("failed", "exit 1")
    assert db.execute("SELECT tokens_in,tokens_out,cached_tokens FROM trace_usage").fetchone() == (11, 4, 2)
    assert db.execute("SELECT active_branch FROM trace_messages WHERE source_id='branch'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM trace_messages WHERE source_id IN ('compact','sub')").fetchone()[0] == 0
    assert not result["warnings"]  # known compaction and parent/child markers are recognized


def test_same_fingerprint_is_idempotent(tmp_path):
    path = tmp_path / "transcript.jsonl"; write_jsonl(path, stock_rows()); db = sqlite3.connect(":memory:")
    assert import_claude_code_jsonl(db, path)["status"] == "imported"
    assert import_claude_code_jsonl(db, path)["status"] == "noop"


def test_changed_fingerprint_refuses_overwrite(tmp_path):
    path = tmp_path / "transcript.jsonl"; write_jsonl(path, stock_rows()); db = sqlite3.connect(":memory:")
    import_claude_code_jsonl(db, path); path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="fingerprint changed"):
        import_claude_code_jsonl(db, path)


def test_parse_error_is_recorded_and_stays_loud_on_retry(tmp_path):
    path = tmp_path / "bad.jsonl"; path.write_text("{bad}\n"); db = sqlite3.connect(":memory:")
    with pytest.raises(ValueError, match="line 1"):
        import_claude_code_jsonl(db, path, "s1")
    assert db.execute("SELECT status FROM trace_imports").fetchone()[0] == "failed"
    with pytest.raises(ValueError, match="line 1"):
        import_claude_code_jsonl(db, path, "s1")


def test_non_object_record_is_normalized_to_value_error_and_recorded(tmp_path):
    path = tmp_path / "bad-object.jsonl"; path.write_text("[]\n"); db = sqlite3.connect(":memory:")
    with pytest.raises(ValueError, match="not an object"):
        import_claude_code_jsonl(db, path, "s1")
    assert db.execute("SELECT status FROM trace_imports").fetchone()[0] == "failed"


def test_inactive_sidechain_excluded_from_usage_and_tools(tmp_path):
    rows = stock_rows()
    rows[3]["message"] = {"role": "assistant", "content": [{"type": "tool_use", "id": "dead-tool", "name": "Bash"}], "usage": {"input_tokens": 100, "output_tokens": 50}}
    rows.append({"type": "user", "uuid": "dead-result", "sessionId": "s1", "isSidechain": True, "message": {"content": [{"type": "tool_result", "tool_use_id": "dead-tool", "is_error": True, "content": "dead branch"}]}})
    path = tmp_path / "transcript.jsonl"; write_jsonl(path, rows); db = sqlite3.connect(":memory:")
    import_claude_code_jsonl(db, path)
    assert db.execute("SELECT tokens_in,tokens_out FROM trace_usage").fetchone() == (11, 4)
    assert db.execute("SELECT COUNT(*) FROM trace_tool_calls WHERE source_id='dead-tool'").fetchone()[0] == 0


def test_unknown_record_is_warning_not_guess(tmp_path):
    rows = stock_rows() + [{"type": "future-thing", "uuid": "x", "sessionId": "s1"}]
    path = tmp_path / "transcript.jsonl"; write_jsonl(path, rows); db = sqlite3.connect(":memory:")
    assert "unsupported record" in import_claude_code_jsonl(db, path)["warnings"][0]


def test_analysis_threshold_gate_is_quiet_below_minimum(tmp_path):
    db = sqlite3.connect(":memory:"); db.execute("CREATE TABLE outcomes (id TEXT, status TEXT)")
    db.execute("INSERT INTO outcomes VALUES ('o1','done')")
    output_root = tmp_path / "analysis"
    result = analyze(db, output_root, min_completed=2)
    assert result["status"] == "quiet" and result["completed_outcomes"] == 1
    assert not any(result["queues"].values()) and not output_root.exists()


def test_analysis_emits_review_queues_only_without_touching_memory_or_routing(tmp_path):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE outcomes (id TEXT, status TEXT)")
    db.executemany("INSERT INTO outcomes VALUES (?, 'done')", [("o1",), ("o2",)])
    db.execute("CREATE TABLE notes (id TEXT PRIMARY KEY, body TEXT)")
    db.execute("INSERT INTO notes VALUES ('n1','keep me')")
    routing = tmp_path / "routing.toml"; tmp_path.mkdir(); routing.write_text("[routing]\n")
    from above_all.traces import ensure_trace_schema
    ensure_trace_schema(db)
    db.executemany("INSERT INTO trace_tool_calls VALUES (?,?,?,?,?,?,?,?)", [("t1","s1",1,"Bash",None,"failed",None,None), ("t2","s2",1,"Bash",None,"failed",None,None)])
    before = routing.read_bytes()
    result = analyze(db, tmp_path / "analysis", min_completed=2)
    assert result["status"] == "proposed" and result["queues"]["system"]
    assert sorted(p.name for p in (tmp_path / "analysis").iterdir()) == ["eval-candidates.json", "system-candidates.json", "user-candidates.json"]
    assert db.execute("SELECT body FROM notes").fetchone()[0] == "keep me"
    assert routing.read_bytes() == before
