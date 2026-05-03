# Changelog

## Unreleased

### Scoring v2 — top-5 benchmarks with median imputation (Node)

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

**Python parity.** `packages/python/tryaii_dre/scoring/engine.py` still
uses the old top-3 + silent-skip behaviour. A TODO comment now points at
the Node implementation. Mirroring this change in Python is a follow-up.

## 0.1.0 (2026-03-29)

- Initial release as TryAii-DRE monorepo (ported from diffrential)
- Python and Node core packages with routing engine support
- 35+ models from 6 providers (OpenAI, Anthropic, Google, xAI, DeepSeek, Mistral)
- 12 standard benchmarks (MMLU, HumanEval, SWE-bench, GSM8K, MT-Bench, etc.)
- Embedding-based semantic classification with keyword fallback
- 3-factor scoring engine (quality, cost, speed) with user priorities
- OpenRouter active routing integration
- CLI tool (tryaii-dre route, models, benchmarks, setup, regenerate)
- LRU cache with TTL for embeddings and classifications
- Pre-computed centroids for zero first-run delay
