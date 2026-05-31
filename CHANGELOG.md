# Changelog

## 0.2.1 (2026-05-31)

Bugfix release. **The 0.2.0 PyPI wheel was broken and has been yanked** — please
use 0.2.1. (The npm 0.2.0 package was unaffected; 0.2.1 is published for parity.)

### Fixed

- **Missing `tryaii_dre.cache` submodule in the published wheel.** The root
  `.gitignore` had an unanchored `cache/` pattern (intended for the repo-root
  benchmark-snapshot cache). Hatchling honors `.gitignore` at build time, so the
  pattern also matched `packages/python/tryaii_dre/cache/` and silently dropped
  it from the sdist and wheel — a clean `pip install tryaii-dre` followed by
  `from tryaii_dre import Router` raised
  `ModuleNotFoundError: No module named 'tryaii_dre.cache'`. The patterns are now
  anchored (`/cache/`, `/cache-shared/`) so only the repo-root directories are
  ignored, and the wheel ships all 33 modules. npm was unaffected (it ships
  `dist/` via the `files` field, not `.gitignore`).

### Changed

- Python CI lint is green again. `UP045` (`Optional[X]` → `X | None`) is ignored
  in ruff config because the package targets Python 3.9 + pydantic, where that
  union syntax raises `TypeError` at annotation-evaluation time; the remaining
  `E402`/`E501`/`F841` findings were cleaned up.

## 0.2.0 (2026-05-30)

First public release on PyPI and npm (the 0.1.0 monorepo below was never
published to a public registry). Highlights: full Node/Python routing parity, a
matching `tryaii-dre` CLI on both packages, and scoring v2 in both SDKs.

### Node/Python SDK parity reconciled

A cross-SDK audit found the Node and Python routers had silently drifted despite
being meant to produce identical routing/scoring/budget decisions. Reconciled to
a single set of canonical rules:

- **Preset data** is now byte-identical across both packages (Python
  `default_models.json` is the source of truth; the Node copy was corrected).
- **Speed scores, cost/speed gating, priorities clamping (round-half-up), and
  tie-breaks** (utility → lowest cost → smallest model id) now match.
- **Budget feasibility** is decided in float; the integer DP only optimizes and
  falls back to the cheapest assignment rather than declaring a feasible dataset
  infeasible.
- **No-benchmark-signal fallback**: prompts whose embedding is orthogonal to
  every centroid stay routable on cost/speed instead of crashing `route()`.
- A `tests/test_parity.py` guard asserts the preset JSONs and normalization
  ranges stay in sync.

### CLI parity — one `tryaii-dre` command on both packages

Both SDKs now ship a matching `tryaii-dre` CLI (`route`, `eval`, `models`,
`benchmarks`, `setup`, `regenerate`) with identical `eval` artifacts
(`results.jsonl` + `summary.json` + `index.html`). Each opens with an animated
blue→red banner printed to stderr that self-suppresses on non-TTY / `NO_COLOR` /
`TRYAII_NO_BANNER` / `--no-banner`.

### Scoring v2 — top-5 benchmarks with median imputation

`packages/node/src/scoring/engine.ts`

Routing now considers the prompt's top-5 most-similar benchmarks (was 3)
and fills missing benchmark data with the **registry-wide median** for
each benchmark instead of silently dropping it.

**Why.** The old behaviour produced an unintuitive failure mode: a model
with sparse benchmark data could outrank a fully-covered model because
missing benchmarks were erased from both the numerator and denominator of
the weighted-quality average. A model with `{HumanEval: 95}` only would
score 100% on the one benchmark it had, while a model with
`{HumanEval: 95, LiveBench: 60}` got dragged down by including LiveBench —
so the broader-coverage model lost.

Observed in the eval harness at `quality=5/cost=1/speed=1`: `grok-4-latest`
(no LiveBench score) was picked for ~84% of coding prompts, beating
`gpt-5.2` (HumanEval 95, LiveBench 78) purely because grok's matched
LiveBench similarity was being dropped from its quality average. After the
fix, gpt-5.2 wins coding under quality-first priorities.

**What changed.**
- `TOP_BENCHMARKS_FOR_SCORING` raised from 3 to 5.
- Missing-benchmark data is imputed from the registry median instead of
  being silently skipped. Median is computed per `scoreModels` call from
  the same `models[]` argument the engine is about to score, so model
  filters flow through correctly.
- Imputation is neutral — sparse data is treated as "average", not zero.
  The previous free-pass effect goes away because the imputed value
  participates in the score instead of vanishing.
- The `bestReasoning` string appends `imputed: N/5` whenever any
  benchmark in the top-5 was imputed for that model, so eval output
  surfaces which decisions involved an estimate vs. real data.
- If **no** model in the registry has data on a benchmark, the existing
  "skip the model entirely if it intersects nothing" path is preserved
  (imputation needs data to estimate from). The test pinning that
  behaviour at `tests/scoring/engine.test.ts:137` still passes.

**API surface.** Unchanged. `RouteResult`, `ModelScore`, and
`ClassificationResult` shapes are the same; no call-site needs to change.

**Behavioural impact.** Routing decisions for prompts whose top-5
benchmarks include sparse entries (LiveBench, SWE-bench, MT-Bench, etc.)
will shift toward models with broader benchmark coverage. The shift is
most visible at quality-heavy priorities; balanced priorities are less
affected because cost and speed dampen swings.

**Python parity.** Mirrored in `packages/python/tryaii_dre/scoring/engine.py` —
both SDKs now use top-5 + median imputation with matching tie-breaks.

## 0.1.0 (2026-03-29)

- Initial monorepo release (ported from diffrential); never published to a public registry
- Python and Node core packages with routing engine support
- 35+ models from 6 providers (OpenAI, Anthropic, Google, xAI, DeepSeek, Mistral)
- 12 standard benchmarks (MMLU, HumanEval, SWE-bench, GSM8K, MT-Bench, etc.)
- Embedding-based semantic classification with keyword fallback
- 3-factor scoring engine (quality, cost, speed) with user priorities
- OpenRouter active routing integration
- CLI tool (tryaii-dre route, models, benchmarks, setup, regenerate)
- LRU cache with TTL for embeddings and classifications
- Pre-computed centroids for zero first-run delay
