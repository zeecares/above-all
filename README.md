# above-all

A small, provider-neutral control plane for a personal assistant that sits above disposable agent sessions. It keeps durable work state separate from curated knowledge, merges global and project context, and routes each step by judgment level rather than task label.

## What works today

Weekend 3 adds one narrow, tested trace plane:

- a stock public Claude Code JSONL adapter normalizes messages, tool calls/results, token usage, sidechain markers, and known compaction/subagent records into the four-table trace schema
- transcript import is idempotent by path and SHA-256 fingerprint; changed sources and parse errors fail loudly instead of overwriting history
- wrapper exit imports only an explicit `transcript.jsonl` or `claude-code.jsonl` owned by that session; internal Claude Code and pi formats remain unverified and disabled
- `above-all analyze` is a quiet, SQL-first scaffold that emits system/user/eval candidate queues only after enough completed outcomes; it changes no routing or memory
- model-neutral project skills live at `.above-all/skills/<name>/SKILL.md`, shadow global skills, validate `name` and the description/when-to-use trigger, and are injected only through explicit `--skill` selection

Weekend 2 provides the headless/interactive session wrapper, handoff envelope, bounded approved context, session/outcome recording in both scopes, and candidate-only exit harvesting. The `MemoryBackend` interface keeps SQLite/FTS as the reference backend, with manual approve/replace/discard and preserved superseded evidence.

Weekend 1 established CLI/project resolution, migrations, the work-state plane, OKF-lite notes, FTS5 with staleness filtering, and editable routing.

Run locally:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
above-all init
above-all scope
above-all skills
above-all analyze
pytest
ruff check .
```

No credentials or provider-specific endpoints are committed. The supported adapter targets the stock public Claude Code format only; confirm internal-build transcript shape before enabling it.

## Storage model

Work state stays in SQLite. Knowledge stays in Markdown and is indexed into SQLite for search. The global scope owns cross-project outcomes, sessions, decisions, and events; the project scope owns project outcomes, decisions, sessions, events, and notes. When IDs collide, the project row overlays the global row.

## Next

Weekend 4 adds reviewed consolidation and restrained proactivity. Their modules exist only as explicit stubs today so callers can see the intended boundaries without mistaking them for working features. Agent CLI commands default to placeholders in `config/agent.example.toml`; copy it to `~/.above-all/agent.toml` and map it to your environment, or pass commands explicitly after `--`.


