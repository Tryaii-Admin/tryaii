# Scoring

`ScoringEngine` turns a prompt's benchmark similarities + the model catalog + your [priorities](priorities.md) into ranked `ModelScore`s. You rarely call it directly (the Router does), but its exports are public for custom pipelines and normalization tweaks.

Imports — Node: all from package root. Python: `from tryaii.scoring import ScoringEngine, BenchmarkNormalizer, NORMALIZATION_RANGES` (`ModelScore` from `tryaii.scoring.engine`).

## The algorithm (what the numbers mean)

1. Take the prompt's **top 5** most-similar benchmarks (from [classification](classification.md)).
2. **Quality score** (0–1): similarity-weighted average of the model's normalized scores on those benchmarks. Missing scores are imputed from the registry-wide median for that benchmark (reasoning strings show `| imputed: n/m`). A model intersecting none of the top benchmarks is dropped — unless *all* models are signal-less, in which case everyone is rescored at neutral quality 0.5 with reasoning `"No benchmark signal -- routed on cost/speed"`.
3. **Cost score** (0–1, higher = cheaper): `max(0, 1 − avgCostPer1k / 0.1)` — $0.10/1k is the zero point; no pricing → 0.
4. **Speed score** (0–1): from the latency tier via `SPEED_SCORES`: `very fast 1.0, fast 0.8, medium 0.6, slow 0.3, very slow 0.1` (unknown tier 0.3, no latency 0).
5. **Final score**: weighted mean using the priority weights, then — when there's more than one candidate — min-max rescaled into **0.1–0.95** (all-equal → 0.5). Single-candidate results keep their raw weighted score. Scores are therefore *relative within one call*, not comparable across calls.
6. Deterministic ordering: final score desc → more real (non-imputed) benchmark coverage → model ID ascending.

## ModelScore

| Python | Node | |
|---|---|---|
| `model_id` | `modelId` | |
| `final_score` | `finalScore` | 0–1, relative (see above) |
| `quality_score` / `cost_score` / `speed_score` | `qualityScore` / `costScore` / `speedScore` | Component scores |
| `quality_contribution` / `cost_contribution` / `speed_contribution` | `…Contribution` | score × weight |
| `top_benchmarks` | `topBenchmarks` | The model's own (non-imputed) relevant benchmarks, normalized |
| `reasoning` | `reasoning` | e.g. `Quality: 0.82 on [HumanEval (91%), SWE-bench (74%)] | Cost efficiency: 0.95 | Speed: 0.80 (fast)` |

Note: the [DREClient](../client/README.md)'s Node result type drops the contribution/`topBenchmarks` fields (exported as `ClientModelScore`).

## Normalization

Raw benchmark scores live on different scales; `BenchmarkNormalizer` maps them to 0–1:

```ts
const n = new BenchmarkNormalizer();            // seeded with NORMALIZATION_RANGES
n.normalize('MT-Bench', 8.3);                   // (8.3-5)/(10-5) → 0.66
n.registerRange('MyBench', 0, 50, 'optional description');
```

`NORMALIZATION_RANGES` (identical in both SDKs): MMLU 25–95, HellaSwag 50–98, HumanEval 20–95, SWE-bench 5–85, TruthfulQA 20–85, ARC 0–95, GSM8K 20–98, DROP 30–90, SuperGLUE 40–95, Chatbot Arena (LMSys) 1000–1550, MT-Bench 5–10, LiveBench 0–100. Unknown benchmarks are assumed to be 0–100 percentages.

## Direct use

```python
from tryaii.scoring import ScoringEngine
scores = ScoringEngine().score_models(models, benchmark_similarities,
                                      priorities=Priorities.performance(), top_k=5)
```

```ts
import { ScoringEngine } from 'tryaii';
const scores = new ScoringEngine().scoreModels(models, benchmarkSimilarities, priorities, 5);
```
