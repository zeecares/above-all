from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from above_all.db import GLOBAL_MIGRATIONS, migrate
from above_all.trace_export import ExportSelection, export_traces, redact
from above_all.traces import parse_claude_code_jsonl


def _session(db: sqlite3.Connection, tmp_path: Path, sid: str, *, ended=True, outcome="o1", project="p"):
    source = tmp_path / sid / "transcript.jsonl"
    source.parent.mkdir()
    rows = [
        {"type":"user","uuid":"u1","sessionId":sid,"timestamp":"2026-01-01T00:00:00Z","message":{"content":"read /Users/alice/work and token=abc123456789"}},
        {"type":"assistant","uuid":"a1","sessionId":sid,"timestamp":"2026-01-01T00:01:00Z","message":{"content":[{"type":"text","text":"done"}],"usage":{"input_tokens":1,"output_tokens":2}}},
    ]
    source.write_text("".join(json.dumps(x)+"\n" for x in rows))
    db.execute("INSERT INTO sessions (id,provider,project,mode,outcome_id,started_at,ended_at,source_path) VALUES (?,?,?,?,?,?,?,?)", (sid,"claude-code",project,"work",outcome,"2026-01-01T00:00:00Z","2026-01-01T00:02:00Z" if ended else None,str(source.parent)))
    db.commit()
    return source


def test_export_is_scoped_redacted_deterministic_and_parseable(tmp_path):
    db = migrate(tmp_path / "db.sqlite", GLOBAL_MIGRATIONS)
    source = _session(db,tmp_path,"wanted")
    _session(db,tmp_path,"unrelated")
    out = tmp_path / "export"
    manifest = export_traces([(db,"global",None)],out,ExportSelection(session_ids=("wanted",)),home=Path("/Users/alice"),env_values=())
    assert sorted(p.name for p in out.iterdir()) == ["manifest.json","session-0001.jsonl"]
    text=(out/"session-0001.jsonl").read_text()
    assert "/Users/alice" not in text and "abc123456789" not in text
    assert "~/work" in text and "[REDACTED]" in text
    parsed=parse_claude_code_jsonl(out/"session-0001.jsonl")
    assert parsed.session_id == "wanted"
    assert manifest["schema_version"] == 1 and manifest["redaction_count"] == 2
    assert manifest["sessions"][0]["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    first=(out/"session-0001.jsonl").read_bytes(); first_manifest=(out/"manifest.json").read_bytes()
    out2=tmp_path/"export2"
    export_traces([(db,"global",None)],out2,ExportSelection(session_ids=("wanted",)),home=Path("/Users/alice"),env_values=())
    assert first == (out2/"session-0001.jsonl").read_bytes()
    # Selection is included, so normalize the requested output-independent manifest directly.
    assert first_manifest == (out2/"manifest.json").read_bytes()


def test_requires_scope_completed_sessions_and_no_overwrite(tmp_path):
    db=migrate(tmp_path/"db.sqlite",GLOBAL_MIGRATIONS)
    _session(db,tmp_path,"open",ended=False)
    with pytest.raises(ValueError, match="explicit scope"):
        export_traces([(db,"global",None)],tmp_path/"a",ExportSelection())
    with pytest.raises(ValueError, match="not found"):
        export_traces([(db,"global",None)],tmp_path/"b",ExportSelection(session_ids=("open",)))
    existing=tmp_path/"existing"; existing.mkdir()
    with pytest.raises(FileExistsError):
        export_traces([(db,"global",None)],existing,ExportSelection(session_ids=("open",)))


def test_filters_and_secret_key_redaction(tmp_path):
    db=migrate(tmp_path/"db.sqlite",GLOBAL_MIGRATIONS)
    _session(db,tmp_path,"one",outcome="o1",project="alpha")
    _session(db,tmp_path,"two",outcome="o2",project="beta")
    clean,count=redact({"api_key":"value","nested":["Bearer abcdefghijk"]},Path("/home/x"),())
    assert clean == {"api_key":"[REDACTED]","nested":["Bearer [REDACTED]"]} and count == 2
    manifest=export_traces([(db,"global",None)],tmp_path/"out",ExportSelection(outcome_id="o2",project="beta",since="2025",until="2027"),env_values=())
    assert [x["session_id"] for x in manifest["sessions"]] == ["two"]


def test_session_id_cannot_escape_output_and_author_is_not_secret(tmp_path):
    db=migrate(tmp_path/"db.sqlite",GLOBAL_MIGRATIONS)
    _session(db,tmp_path,"../escape")
    out=tmp_path/"out"
    manifest=export_traces([(db,"global",None)],out,ExportSelection(session_ids=("../escape",)),env_values=())
    assert manifest["sessions"][0]["file"] == "session-0001.jsonl"
    clean,count=redact({"author":"alice","auth_token":"secret-value"},Path("/home/x"),())
    assert clean == {"author":"alice","auth_token":"[REDACTED]"} and count == 1


def test_project_store_overlays_global_on_id_collision(tmp_path):
    global_db=migrate(tmp_path/"g.sqlite",GLOBAL_MIGRATIONS)
    from above_all.db import PROJECT_MIGRATIONS
    project_db=migrate(tmp_path/"p.sqlite",PROJECT_MIGRATIONS)
    gsrc=_session(global_db,tmp_path,"dup")
    psrc=tmp_path/"dup-proj"/"transcript.jsonl"; psrc.parent.mkdir()
    psrc.write_text(json.dumps({"type":"user","uuid":"u9","sessionId":"dup","timestamp":"2026-01-01T00:00:00Z","message":{"content":"project version"}})+"\n")
    project_db.execute("INSERT INTO sessions (id,provider,mode,outcome_id,started_at,ended_at,source_path) VALUES (?,?,?,?,?,?,?)",("dup","claude-code","work","o1","2026-01-01T00:00:00Z","2026-01-01T00:02:00Z",str(psrc.parent)))
    project_db.commit()
    manifest=export_traces([(global_db,"global",None),(project_db,"project","myproj")],tmp_path/"out",ExportSelection(session_ids=("dup",)),env_values=())
    assert manifest["sessions"][0]["scope"]=="project" and manifest["sessions"][0]["project"]=="myproj"
    text=(tmp_path/"out"/"session-0001.jsonl").read_text()
    assert "project version" in text and gsrc.read_text() not in text


def test_redaction_covers_authorization_plural_nested_and_serialized_json(tmp_path):
    clean,count=redact({
        "authorization":"Basic abcdef123456",
        "tokens":["opaque-tok-one","opaque-tok-two"],
        "credentials":{"nested_password":"hunter2hunter"},
        "authors":"bob",
        "author":"alice",
        "usage":{"input_tokens":5,"output_tokens":7},
        "note":'{"token": "opaquevalue123"}',
        "escaped":'\\"secret\\": \\"anothervalue456\\"',
    },Path("/home/x"),())
    assert clean["authorization"]=="[REDACTED]"
    assert clean["tokens"]==["[REDACTED]","[REDACTED]"]
    assert clean["credentials"]=={"nested_password":"[REDACTED]"}
    assert clean["authors"]=="bob" and clean["author"]=="alice"
    assert clean["usage"]=={"input_tokens":5,"output_tokens":7}
    assert "opaquevalue123" not in clean["note"] and "[REDACTED]" in clean["note"]
    assert "anothervalue456" not in clean["escaped"]
    assert count==6
    json.loads(clean["note"])


def test_missing_source_and_invalid_json_clean_up_partial_export(tmp_path):
    db=migrate(tmp_path/"db.sqlite",GLOBAL_MIGRATIONS)
    db.execute("INSERT INTO sessions (id,provider,project,mode,outcome_id,started_at,ended_at,source_path) VALUES (?,?,?,?,?,?,?,?)",("ghost","claude-code","p","work","o1","2026-01-01T00:00:00Z","2026-01-01T00:02:00Z",str(tmp_path/"nowhere")))
    db.commit()
    out=tmp_path/"out"
    with pytest.raises(FileNotFoundError):
        export_traces([(db,"global",None)],out,ExportSelection(session_ids=("ghost",)),env_values=())
    assert not out.exists()
    bad=_session(db,tmp_path,"badjson")
    bad.write_text('{"type":"user"}\nnot json\n')
    with pytest.raises(ValueError, match="invalid JSON"):
        export_traces([(db,"global",None)],out,ExportSelection(session_ids=("badjson",)),env_values=())
    assert not out.exists()


def test_unknown_explicit_session_id_refused(tmp_path):
    db=migrate(tmp_path/"db.sqlite",GLOBAL_MIGRATIONS)
    _session(db,tmp_path,"real")
    with pytest.raises(ValueError, match="not found"):
        export_traces([(db,"global",None)],tmp_path/"out",ExportSelection(session_ids=("real","nosuch")),env_values=())
    assert not (tmp_path/"out").exists()
