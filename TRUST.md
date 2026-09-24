# Trust

The short version: the safety machinery is tested; the product behavior is unproven on real workloads. Trust it as engineering, not yet as a product.

## Proven

- 250 tests green (v0.1.0); CI runs ruff and pytest on Python 3.10 and 3.12 for every push and PR.
- Replay harness: phase-checkpointed runner (ingest, index, retrieve, answer, evaluate), 13 fixtures pinned by sha256 in `tests/fixtures/replay/fixture-set.json` over a hashed trace corpus. Tampering with a fixture fails the run. Descriptive and associative recall are scored separately.
- Held-out eval thresholds (`tests/fixtures/eval-thresholds.json`): zero stale or cross-project leakage, zero unsupported claims, factual accuracy and abstention accuracy 1.0, recall@k and MRR 1.0, precision@k at least 0.65.
- Write gate: sessions and analysis passes produce candidates and proposed changesets only. Promotion into active memory or routing requires explicit human approval. Promotion writes are atomic, journaled, and recoverable.
- Managed delivery: approved notes are written only inside hash-marked managed sections of provider context files, with a size budget and an expiry. Drift inside the section is refused, and `above-all deliver --check` audits the state.
- Privacy: project state is git-ignored by default; `--share-approved` generates exact exceptions for approved notes only; `above-all privacy-check` validates tracked files against the policy. Trace export is scoped, schema-versioned, and best-effort redacted.

## Not proven

- Real-workload behavior. Every replay fixture is synthetic; the harness has never run against real user sessions. The tests prove the machinery works, not that the agent behaves well on real data.
- Transcript formats beyond the stock public Claude Code JSONL. Unverified providers stay disabled rather than guessed.
- Model-backed features: the value gate and model routing fail closed until real endpoints are configured.
- The mem0_oss and supermemory_local backends: blocked on deployment policy and fail loudly if requested.
- Hybrid retrieval: measured worse than the baseline and ships off by default.

## Invariants

These must never break. A change that breaks one is a bug, regardless of intent.

1. Nothing edits active memory or routing without explicit approval. Sessions, harvest, consolidation, and analysis produce candidates only.
2. Writes are confined to the tool's own stores (`~/.above-all` or `$ABOVE_ALL_HOME`, and project `.above-all/`) and to the marked managed section of provider context files. Content outside the markers is never edited.
3. Drift inside the managed section is refused, never silently overwritten.
4. Provider memory import is review-only.
5. Unknown providers, backends, or transcript formats fail loudly. No partial imports, no guessing.
6. Trace import is idempotent by path and SHA-256; changed sources and parse errors fail loudly instead of overwriting history.
7. Proactivity fails closed: anything that cannot name a decision, risk, or saved step stays internal.
8. Private state is never tracked; privacy-check must pass before anything is shared.

## Measurement

Behavior is measured with the replay runner and the eval harness: deterministic metrics for retrieval, leakage, staleness, latency, tokens, and cost. Subjective answer quality is the only LLM-judged surface. New fixtures must be sha256-pinned and labeled with corpus provenance; synthetic data must never be presented as harvested, and a regression test enforces that.

## Removal

1. `pip uninstall above-all`
2. Delete the global store (`~/.above-all`, or `$ABOVE_ALL_HOME` if overridden) and any project `.above-all/` directories.
3. Delete the managed section between the `<!-- above-all:managed begin -->` and `<!-- above-all:managed end -->` markers in any provider context file the tool delivered to. Everything outside the markers is yours and untouched.
4. Remove any external scheduler entries you added for maintenance or watches; the tool itself runs no daemon.

Verify removal: `grep -r "above-all:managed"` across your repos returns nothing, both stores are gone, and `above-all` is not found.
