# above-all

A small, provider-neutral control plane for a personal assistant that sits above disposable agent sessions. It keeps durable work state separate from curated knowledge, merges global and project context, and routes each step by judgment level rather than task label.

## What works today

Conflict-free memory delivery (ZEE-64) makes above-all the single durable owner of curated memory while agent CLIs stay delivery surfaces:

- `above-all deliver --provider claude-code` writes the same approved global/project notes into a clearly marked managed section of the provider's native context file (CLAUDE.md), with a manifest recording source note IDs, a content hash, a size budget, and an expiry
- content outside the managed markers is user-owned and never edited; an existing provider file is adopted, not overwritten
- `above-all deliver --check` reports clean, drifted, expired, missing, or unmanaged; drift inside the managed section is refused, never silently overwritten (`--force` regenerates after review)
- `above-all import --provider claude-code` reads provider-native memory as review-only candidates with provider/file provenance, deduplicated against active notes, the knowledge map, and pending candidates; nothing is promoted without the existing human review path
- providers register only after their native format is verified; pi stays disabled until a real fixture confirms it (ZEE-57), and unknown providers fail loudly

Weekend 4 adds reviewed consolidation and restrained proactivity:

- a daily expiry sweep marks stale active notes `needs-review`, removes them from FTS, and never touches candidates
- the bounded weekly pass proposes a changeset for duplicates, contradictions, reviewed promotions, and Weekend 3 trace-analysis queues; it cannot apply without explicit approval and preserves superseded/contradictory evidence
- pollution metrics expose admission/use, recent replacement/contradiction, retrieval-recall, prompt-size/token/cost, and loud extraction-failure tripwires without calling a model directly
- persisted clock, cadence, event, and deadline watches fire internal events; one fail-closed value gate uses the model-routing placeholder to require a classified decision, risk, or saved step; unclassified and internal-only items remain logged and internal
- all learned changes remain proposals; routing and active memory change only through the existing human review path

Weekend 3 adds one narrow, tested trace plane:

- a stock public Claude Code JSONL adapter normalizes messages, tool calls/results, token usage, active-branch messages, tool calls/results, token usage, and recognizes known compaction/subagent marker records into the four-table trace schema
- transcript import is idempotent by path and SHA-256 fingerprint; changed sources and parse errors fail loudly instead of overwriting history
- wrapper exit imports only an explicit `transcript.jsonl` or `claude-code.jsonl` owned by that session; internal Claude Code and pi formats remain unverified and disabled
- `above-all analyze` is a quiet, SQL-first scaffold that emits system/user/eval candidate queues only after enough completed outcomes; it changes no routing or memory
- model-neutral project skills live at `.above-all/skills/<name>/SKILL.md`, shadow global skills, validate `name` and the description/when-to-use trigger, and are injected through a session-scoped file only after explicit `--skill` selection

Session memory extraction is bounded and review-only. For explicit stock Claude Code traces, exit harvest proposes a candidate from useful fact/procedure text in the final assistant answer and suppresses wrapper boilerplate or empty output. The deterministic fallback is deliberately narrow: it cannot infer a lesson hidden only in tool calls or intermediate reasoning. A provider-neutral routed-model seam may supply deeper extraction, but its output is still only a source-anchored candidate and provider failure falls back deterministically.

Weekend 2 provides the headless/interactive session wrapper, handoff envelope, bounded approved context, session/outcome recording in both scopes, and candidate-only exit harvesting. The `MemoryBackend` interface keeps SQLite/FTS as the reference backend, with manual approve/replace/discard and preserved superseded evidence.

Weekend 1 established CLI/project resolution, migrations, the work-state plane, OKF-lite notes, FTS5 with staleness filtering, and editable routing.

Run locally:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
above-all init
# Or explicitly share reviewed notes/context while private state stays ignored:
above-all init --share-approved
above-all privacy-check --share-approved
above-all scope
above-all skills
above-all analyze
pytest
ruff check .
```

Project init writes a tracked `.above-all/.gitignore`. The default keeps the whole project scope local. `--share-approved` generates exact exceptions only for active, valid 32-hex-ID notes and generated context; rerun init after approving a new note to refresh that list. Databases, candidates, sessions, traces, logs, selected-skill files, unknown future paths, and malformed or inactive notes stay ignored. Init refuses tracked state outside the selected public surface. `above-all privacy-check [--share-approved]` validates both tracked files and the installed policy before a commit.

No credentials or provider-specific endpoints are committed. The supported adapter targets the stock public Claude Code format only; confirm internal-build transcript shape before enabling it.

## Storage model

Work state stays in SQLite. Knowledge stays in Markdown and is indexed into SQLite for search. The global scope owns cross-project outcomes, sessions, decisions, and events; the project scope owns project outcomes, decisions, sessions, events, and notes. When IDs collide, the project row overlays the global row.

Reviewed notes also mirror into a small SQLite knowledge map: claims distinguish premises from inferences and carry confidence, scope, freshness, exact source anchors, optional entities/relations, and supersession/contradiction edges. The map is updated only by the existing note index inside the candidate review gate. It is not a second write path and does not infer facts. `above-all memory why <query>` (or `memory explain`) returns the active, fresh claim plus its evidence path. Map-assisted retrieval is not enabled by default; it must beat the held-out eval baseline before serving ordinary search.

## Next

Agent CLI commands default to placeholders in `config/agent.example.toml`; copy it to `~/.above-all/agent.toml` and map it to your environment, or pass commands explicitly after `--`.






## Held-out evaluation

`above-all eval` runs a provider-neutral, held-out scorecard before retrieval changes ship:

```bash
above-all eval tests/fixtures/eval-golden.json \
  --thresholds tests/fixtures/eval-thresholds.json \
  --output eval-baseline.json
```

The deterministic CI path reports precision@k, true labeled recall@k, MRR,
stale and cross-project leakage, plus labeled factual accuracy, unsupported-claim
rate, and abstention accuracy. A fixture can also carry paired with-memory and
no-memory observations for input/output/cached tokens, reported cost, and
latency; these are reported as deltas with descriptive 95% confidence intervals.
They are not called savings.

Answer scoring is deliberately separate from answer generation. CI scores
labeled claims already in a fixture and makes no model call. Real-model replay
can produce those observations later, opt-in, without making CI depend on a
provider. This `recall@k` is true held-out retrieval recall and is distinct from
the existing operational event proxy named `retrieval_recall`.


## Optional measured hybrid retrieval

`sqlite_fts` remains the default. To opt into the experimental local backend, set `memory.backend = "sqlite_hybrid"` in `~/.above-all/agent.toml`. It combines FTS5/BM25 with a 256-float local signed word/character n-gram hash using reciprocal-rank fusion. It makes no API calls and downloads no model. See `LICENSES-HYBRID.md` for the complete component and redistribution inventory.

Measure it on the held-out fixture with:

```bash
above-all eval tests/fixtures/eval-golden.json --compare-backends --json
```

The checked-in golden set does not show a material quality win, so hybrid is not enabled by default. The hash representation can bridge spelling variants and some lexical drift, but it is not general semantic understanding. A larger real, hand-labeled query set is needed before reconsidering the default.


