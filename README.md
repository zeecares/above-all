# above-all

A small, provider-neutral control plane for a personal assistant that sits above disposable agent sessions. It keeps durable work state separate from curated knowledge, merges global and project context, and routes each step by judgment level rather than task label.

## What works today

Weekend 1 establishes the local foundation:

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

Later weekends add the session wrapper, trace importers, reviewed consolidation, native project-context generation, and restrained proactivity. Their modules exist only as explicit stubs today so callers can see the intended boundaries without mistaking them for working features.
