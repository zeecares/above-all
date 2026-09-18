# above-all

A small, provider-neutral control plane for a personal assistant that sits above disposable agent sessions. It keeps durable work state separate from curated knowledge, merges global and project context, and routes each step by judgment level rather than task label.

## What works today

Weekend 2 adds the session wrapper and the memory write gate:

- `above-all do "<intent>" -- <agent CLI command...>` dispatches a headless session with a handoff envelope (`prompt.md` + `envelope.json` in, `result.json` + `diff.patch` + logs out) and records the session and outcome in both scopes
- `above-all work -- <agent CLI command...>` wraps a live interactive session: it regenerates the bounded `AGENT_CONTEXT.md` from approved project notes, hands the session its identity through `ABOVE_ALL_*` environment variables, and captures metadata at exit
- exit harvest always follows the same order - session state, exit summary, memory candidates - and session exit never writes active memory, only candidates
- a `MemoryBackend` interface (`memory_backend.py`) with `sqlite_fts` as the reference backend; `mem0_oss` is a later pluggable candidate pending trace evals
- the candidate queue and manual review flow: `note candidates`, `note approve [--replace <id>]` (replacement keeps the old note as superseded evidence), and `note discard`
- candidate creation failures fail loudly (event row plus stderr warning) instead of silently dropping the batch

Weekend 1 established the local foundation:

- one `above-all` CLI with `init`, `scope`, `route`, `note add`, and `note search`
- a global store at `~/.above-all/` plus a project store at `.above-all/` in the nearest Git worktree
- versioned SQLite migrations for global and project work state
- deterministic global/project merge rules
- Markdown notes with an OKF-lite header, FTS5 search, and staleness filtering at read time
- an editable `routing.toml` that uses placeholder free/frontier endpoints and judgment-level rules
- CI, pytest, and Ruff

Run locally:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
above-all init
above-all scope
pytest
ruff check .
```

No credentials or provider-specific endpoints are committed. Copy `config/routing.example.toml` into the global store as `routing.toml` and replace placeholders locally.

## Storage model

Work state stays in SQLite. Knowledge stays in Markdown and is indexed into SQLite for search. The global scope owns cross-project outcomes, sessions, decisions, and events; the project scope owns project outcomes, decisions, sessions, events, and notes. When IDs collide, the project row overlays the global row.

## Next

Later weekends add trace importers, reviewed consolidation, and restrained proactivity. Their modules exist only as explicit stubs today so callers can see the intended boundaries without mistaking them for working features. Agent CLI commands default to placeholders in `config/agent.example.toml`; copy it to `~/.above-all/agent.toml` and map it to your environment, or pass commands explicitly after `--`.

