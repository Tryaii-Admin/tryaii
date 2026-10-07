# Scoring

`ScoringEngine` turns a prompt's benchmark similarities + the model catalog + your [priorities](priorities.md) into ranked `ModelScore`s. You rarely call it directly (the Router does), but its exports are public for custom pipelines and normalization tweaks.

Imports — Node: all from package root. Python: `from tryaii.scoring import ScoringEngine, ModelScore, BenchmarkNormalizer, NORMALIZATION_RANGES, cost_utility, speed_utility, quality_tolerance, EPS_UNIT, IMPUTED_TERM_WEIGHT, COVERAGE_EXPONENT`.

## The algorithm: `satisficing-v1`

Routing is a **satisficing** decision, not a weighted average. Quality decides *who is allowed to win*; cost and speed decide *which of them does*.

1. Take the prompt's **top 5** most-similar benchmarks (from [classification](classification.md)).
2. **`q'` — quality** (0–1): weighted average of the model's normalized scores over those 5 benchmarks, `q' = Σ(w·norm) / Σw`, where each term's weight folds in prompt relevance, the benchmark's trust weight, **how much of the catalog can be compared on it at all**, and **whether this model's term is evidence or an imputation**:

   ```
   coverage(b) = n_real(b) / N                 # N = routable models (:free excluded)
   w(b, m)     = similarity(b) * BENCHMARK_WEIGHTS[b]
                 * coverage(b) ** COVERAGE_EXPONENT     # COVERAGE_EXPONENT = 1.0
                 * (IMPUTED_TERM_WEIGHT if m's score for b was imputed else 1.0)
                                                        # IMPUTED_TERM_WEIGHT = 0.5
   ```

   `n_real(b)` is how many routable models carry a real, finite score for `b`; it is derived from the live catalog (`ModelRegistry.benchmark_coverage()` / `registry.benchmarkCoverage()`, cached per registry) so a snapshot that adds reporters re-weights them up with no code change, and a benchmark nobody reports has coverage `0.0` and drops out of `q'` entirely. `COVERAGE_EXPONENT` is the dial: 1.0 is linear in coverage, 0.5 halves the effect, 0.0 disables the factor. **Why:** without this factor, a large share of the selected weight can land on benchmarks that only a small minority of the catalog reports — often older benchmarks the current frontier never ran — so a frontier pick ends up judged mostly on imputed values. Weighting each term by coverage and discounting imputed terms keeps the decision on measured evidence and lets more distinct models win, without making routes more expensive or slower. **Benchmark selection is unchanged** (still top 5 by raw similarity), and the reasoning string is unchanged — coverage is visible only on `ModelScore.benchmark_weights` / `benchmarkWeights`, the effective per-term weights to 4 decimals. Missing scores use shrinkage imputation: the model's median normalized level is blended with the registry-wide median, weighting the model's own level by `n / (n + 3)` (reasoning strings disclose `| imputed: n/m`). A model with no usable top-benchmark signal is dropped — unless *all* models are signal-less, in which case everyone is rescored at neutral quality 0.5 with reasoning `"No benchmark signal -- routed on cost/speed"` (that path, and only that path, floors the cost/speed weights at 0.1 so the prompt stays routable).
3. **`U_c` — cost utility** (0–1, higher = cheaper), log-scaled over three decades:

   ```
   p_M = ((input_cost_per_1k + output_cost_per_1k) / 2) * 1000     # avg $ per 1M tokens
   U_c = clamp((ln 50 - ln p_M) / (ln 50 - ln 0.05), 0, 1)
   ```

   `$0.05/M → 1`, `$50/M → 0`, denominator `ln(1000) = 6.907755`, so **every 10× cheaper is +0.3333**. Free is 1.0. A missing or non-finite price is **0.0, not neutral** — a model whose price we do not know must never win a cost comparison — and is flagged `cost: unknown`. (The old `max(0, 1 - avgCostPer1k/0.1)` was linear in dollars and could not tell $0.10/M from $0.50/M at all.)
4. **`U_s` — speed utility** (0–1, higher = faster), log-scaled over two decades of *wait*:

   ```
   T300 = ttft_ms / 1000 + 300 / tokens_per_second      # seconds to a 300-token answer
   U_s  = clamp((ln 30 - ln T300) / (ln 30 - ln 0.3), 0, 1)
   ```

   `0.3 s → 1`, `30 s → 0`, denominator `ln(100) = 4.605170`, so **halving the wait is +0.1505**. Fallbacks, both flagged:

   | situation | behaviour | flag |
   |---|---|---|
   | `tokens_per_second` known, `ttft_ms` missing | `ttft_ms` = the catalog's median TTFT | `ttft: estimated` |
   | `tokens_per_second` missing | `U_s` = the catalog's **p25** of `U_s` (no `T300` is computed) | `speed: unknown` |

   Both fallbacks are computed at runtime from the loaded registry, not hard-coded: `ModelRegistry.speed_stats` / `registrySpeedStats(models)`, cached per registry instance. The p25 choice is deliberately pessimistic — below the catalog median — so a model with no measured throughput can never win a speed-led route.
5. **The band.** `eps` is the quality you are willing to spend to get a cheaper or faster model, in `q'` units:

   ```
   eps = EPS_UNIT * ((cost_priority - 1) + (speed_priority - 1)) / quality_priority      # EPS_UNIT = 0.102
   ```

   | priorities (q,c,s) | `eps` | |
   |---|---|---|
   | (5,1,1) | 0.000 | strict quality: no band, no secondary term |
   | (4,3,3) | 0.102 | |
   | (3,3,3) | 0.136 | "spend up to 0.136 of quality for cheaper/faster" |
   | (2,3,3) | 0.204 | |
   | (3,5,5) | 0.272 | |
   | (2,5,3) | 0.306 | the `budget` preset |
   | (1,5,1) / (1,1,5) | 0.408 | cost only / speed only |

   `eps` is linear in `(c-1)+(s-1)` — the band grows smoothly, never stepwise — and inversely proportional to the quality priority. It is **0 exactly when cost and speed are both 1**, the same condition that switches the secondary term off. **Contenders** = `{ q' >= q'_best - eps }`; a model with no quality signal is never a contender.

   **`EPS_UNIT` is the one tuning dial of this algorithm.** 0.102 was chosen so `eps` at (3,3,3) is a large fraction of the typical quality spread among the strongest catalog models: wide enough that balanced routing genuinely trades a little quality for a much cheaper or faster model, narrow enough that it never drops to a clearly weaker tier. A wider dial buys price with quality, linearly and predictably. Raise it to spend more quality, lower it to spend less — nothing else needs to change when it moves.
6. **The in-band ranker.** `wc = (cost_priority - 1) / 4`, `ws = (speed_priority - 1) / 4` (0 at priority 1, 1.0 at 5). The utilities are first **quantised to 3 decimals, half-to-even** (`U_c_q`, `U_s_q`) so measurement noise in a small tps/ttft sample cannot reorder near-ties inside the band; the reasoning string still shows the unquantised values at 4 dp.

   ```
   sec = (wc * U_c_q + ws * U_s_q) / (wc + ws)      # a weighted AVERAGE, in [0,1]
   ```

   Dividing by `wc + ws` is deliberate: it makes `sec` mean the same thing at (1,5,1), (3,3,3) and (1,1,5), so only the *ratio* of the two weights matters inside the band. The quality priority is **not** a weight in the score — it enters only through `eps`. (`quality_weight` / `qualityWeight` survives for backward-compatible reporting and for the no-signal path.)
7. **`final_score`** — an order-preserving map into `[0, 1]`, rounded half-even to 4 decimals. **There is no min-max rescale any more.**

   ```
   wc + ws == 0  →  final_score = q'                 # strict quality
   in band       →  final_score = 0.5 + 0.5 * sec
   otherwise     →  final_score = 0.5 * q'
   ```

   Contenders therefore occupy `[0.5, 1.0]` and non-contenders `[0.0, 0.5]`: no out-of-band model can ever out-rank a contender, and the split point is itself informative — "did this model clear the quality bar you asked for?"
8. **Deterministic ordering**, one total order in both SDKs: internal utility desc → `q'` desc → count of **real** (non-imputed) scored benchmarks desc → model ID ascending (Unicode code point order, not locale-aware). The evidence term is what stops a model whose score rests entirely on imputation from winning a tie.

### `final_score` is comparable within one call only

`eps` and `q'_best` are properties of the *call*, so the same model can score differently against a different candidate set. The per-model numbers that **are** globally comparable are `quality_score` (`q'`, now that the ranges are catalog-derived rather than per-call), `cost_score` and `speed_score`. Compare models across calls on those, not on `final_score`.

## ModelScore

| Python | Node | |
|---|---|---|
| `model_id` | `modelId` | |
| `final_score` | `finalScore` | 0–1; `>= 0.5` iff the model cleared the band. Within-call only (see above) |
| `quality_score` | `qualityScore` | `q'` — catalog-normalised, imputation-shrunk. Globally comparable |
| `cost_score` | `costScore` | `U_c`. Globally comparable |
| `speed_score` | `speedScore` | `U_s`. Globally comparable |
| `quality_contribution` | `qualityContribution` | `final_score` when `wc+ws == 0`, `0.5 * q'` when out of band, else 0 |
| `cost_contribution` | `costContribution` | `0.5 * wc * U_c_q / (wc + ws)` when in band, else 0 |
| `speed_contribution` | `speedContribution` | `0.5 * ws * U_s_q / (wc + ws)` when in band, else 0 |
| `band_base` | `bandBase` | `0.5` when in band, else `0.0` — present so the three contributions plus this sum to `final_score` |
| `in_band` | `inBand` | bool |
| `benchmark_weights` | `benchmarkWeights` | effective per-term quality weights (similarity × trust × coverage × imputed factor), 4 dp. Diagnostic; not in any wire payload |
| `quality_tolerance` | `qualityTolerance` | the `eps` actually used for this call |
| `quality_best` | `qualityBest` | `q'_best` for this call |
| `signal_flags` | `signalFlags` | subset of `cost: unknown`, `speed: unknown`, `ttft: estimated`, `no benchmark signal` |
| `top_benchmarks` | `topBenchmarks` | The model's own (non-imputed) relevant benchmarks, normalized |
| `reasoning` | `reasoning` | see below |

`quality_contribution + cost_contribution + speed_contribution + band_base == final_score`, exactly before the 4-decimal rounding of the public fields.

Note: the [DREClient](../client/README.md)'s Node result type drops the contribution/`topBenchmarks` fields (exported as `ClientModelScore`).

## The reasoning string

One format, byte-identical in both SDKs, pipe-separated with single spaces and ASCII `--` (never an em dash). A real route at (3,3,3):

```
q'=0.87 (2 real of 5) | imputed: 3/5 | cost 0.2846 ($7.00/M) | speed 0.3477 (6.05 s to 300 tok) | within 0.136 of the best (0.92) | at 3/3/3 you accept up to 0.136 less quality for a cheaper or faster model; this pick gave up 0.06 vs anthropic/claude-fable-5
```

Field order is fixed:

1. `q'={q:.2f} ({n_real} real of {n_top})`, then `| imputed: {imputed}/{n_top}` **only when** `imputed > 0`.
2. `cost {U_c:.4f} (${p_M:,.2f}/M)`, or `cost unknown`.
3. `speed {U_s:.4f} ({T300:.2f} s to 300 tok)`, plus ` [ttft estimated]` when the TTFT was imputed; or `speed unknown (catalog p25)` when there is no throughput measurement.
4. The band clause: `within {eps:.3f} of the best ({q_best:.2f})` in band, `{gap:.3f} below the best ({q_best:.2f}) -- outside the {eps:.3f} tolerance` out of band, or `quality only (no tolerance)` when `eps == 0`.
5. Exactly one exchange-rate sentence, always last: `at {q}/{c}/{s} you accept up to {eps:.3f} less quality for a cheaper or faster model; this pick gave up {regret:.2f} vs {leader_id}`, or `at {q}/{c}/{s} only quality counts` when `eps == 0`. `regret` is `q'_best - q'` **of the selected model**, so every candidate's string names the same leader and the same number — the sentence describes the decision, not the row.

The all-no-signal fallback replaces the whole string with `No benchmark signal -- routed on cost/speed`.

## Normalization

Raw benchmark scores live on different scales; `BenchmarkNormalizer` maps them to 0–1 against a per-benchmark `[min_score, max_score]` window, clamping outliers:

```ts
const n = new BenchmarkNormalizer();            // seeded with NORMALIZATION_RANGES
n.normalize('GPQA', 80.2);                      // (80.2-59.3)/(96.3-59.3) → 0.565 (starter catalog range)
n.registerRange('MyBench', 0, 50, 'optional description');
```

`NORMALIZATION_RANGES` is identical in both SDKs and covers the starter catalog's 16 benchmarks (`BenchmarkNormalizer.from_bundle(bundle)` / `fromBundle(bundle)` gives any other catalog's ranges). It is not a hand-written table of judgement calls — it is **catalog-derived headroom**, fitted to the full catalog and shipped unchanged in every catalog bundle, so a model scores the same on a benchmark both catalogs share:

```
real_b = every non-null, finite raw score of benchmark b across the routable catalog
lo_b   = p25(real_b)        # linear-interpolation percentile
hi_b   = max(real_b)
```

A benchmark with too few real scores keeps a hand-written range and is marked `"fallback": true` in the catalog's `normalization_ranges.json`. Scales are preserved by construction — Arena variants stay Elo ratings (~1200–1550), the rest stay on the scale their source reports (mostly 0–100 percentages) — and unknown benchmarks are still assumed to be 0–100.

**Why p25, and not a floor below every shipping model.** Hand-set floors far below where any real model scores squeeze the entire frontier into a thin sliver near 1.0, so the quality term carries almost no dispersion. Fitting the window to the catalog spreads the frontier out again, which is what makes a band meaningful at all. The trade-off: **a quarter of all real scores now normalise at or near 0 by construction**, so a single floored benchmark is no longer evidence of bad data.

Because the ranges are catalog-dependent, a new catalog release can move every score. Maintainers regenerate them with each catalog release, never together with a routing-logic change, and a range change in the starter catalog is noted in the CHANGELOG.

## Direct use

```python
from tryaii.scoring import ScoringEngine
scores = ScoringEngine().score_models(models, benchmark_similarities,
                                      priorities=Priorities.performance(), top_k=5,
                                      registry_stats=registry.speed_stats)
```

```ts
import { ScoringEngine } from 'tryaii';
const scores = new ScoringEngine().scoreModels(models, benchmarkSimilarities, priorities, 5);
```

`registry_stats` is optional — pass the owning registry's cached stats so a provider or cost filter cannot move the catalog-wide speed fallbacks; omit it and they are computed from the models you passed.
