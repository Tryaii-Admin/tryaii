# Budget routing

`route_dataset_with_budget` / `routeDatasetWithBudget` routes a whole dataset under a shared USD budget: it builds priced candidates for every prompt × model, weights utility by prompt difficulty, and solves a multiple-choice knapsack to **maximize total quality under the budget**. This is what powers [`tryaii eval --max-price`](../../cli/eval/budget-mode/README.md).

```python
from tryaii import Router, Priorities, route_dataset_with_budget

results, optimization = route_dataset_with_budget(
    router=Router(), prompts=[...], priorities=Priorities.balanced(),  # accepted but IGNORED
    max_price=0.50, output_tokens=1000,
    budget_mode="strict",            # or "fit-output"
    difficulty_source="intrinsic",   # "intrinsic" | "capability" | "blend"
    difficulty_gamma=1.0,            # 0 disables difficulty weighting
    progress_callback=lambda done, total: print(done, "/", total))
```

```ts
import { Router, routeDatasetWithBudget } from 'tryaii';

const { results, optimization } = await routeDatasetWithBudget({
  router: new Router(), prompts, priorities,           // priorities ignored (see below)
  maxPrice: 0.5, outputTokens: 1000,
  budgetMode: 'strict', difficultySource: 'intrinsic', difficultyGamma: 1,
  progressCallback: (done, total) => {},
});
```

## How it works

1. Each prompt is routed against **all** models with `Priorities.performance()` (5/1/1) — the passed `priorities` parameter is **ignored**, since cost is handled by the knapsack, not the score. Unpriced models are dropped as candidates.
2. Cost per candidate = `(inputTokens/1000)·input_per_1k + (outputTokens/1000)·output_per_1k`, with input tokens estimated at ~4 chars/token (`estimate_tokens`).
3. Per-prompt **difficulty** (0–1) per `difficulty_source`: `intrinsic` = easy/hard exemplar-centroid logistic from [classification](../routing/classification.md) (falls back to capability when unavailable); `capability` = quality drop from best model to the cheapest tier (`compute_difficulty`); `blend` = mean of both.
4. Difficulties are percentile-ranked across the dataset; each candidate's utility is multiplied by `1 + gamma × rank` — so budget shifts toward prompts where a strong model matters most.
5. Knapsack DP picks one model per prompt (budget discretized into ~10k units). In `fit-output` mode, if the requested `output_tokens` doesn't fit, the largest fitting token count is binary-searched and the optimization re-runs (`effective_output_tokens` reports the reduction).

## Results

- `results`: one `BudgetedRouteResult` per prompt — `route_result`, `selected` (`BudgetCandidate`: `model_id`, `estimated_cost`, `difficulty`, `normal_best_model` = the unconstrained winner, token counts), `cumulative_cost`, `remaining_budget`, `route_ms`.
- `optimization`: `BudgetOptimizationResult` — `status` (`"optimal"` | `"infeasible"`), `total_estimated_cost`, `minimum_required_budget`, `budget`, `budget_mode`, `requested/effective_output_tokens`, `budget_shortfall`, `message`. An infeasible strict run still populates `selected` with the cheapest-per-prompt assignment so you can see the floor.

## Validation errors

`output_tokens < 0`, an unknown `budget_mode`, and `max_price < 0` raise `ValueError`/`Error`. Unknown `difficulty_source` values silently fall back to `intrinsic`.

## Lower-level helpers

Exported for custom pipelines: `estimate_tokens`, `compute_difficulty`, and `DEFAULT_DIFFICULTY_GAMMA` / `DEFAULT_DIFFICULTY_SOURCE` (both SDKs' package roots). Node additionally root-exports `estimateGenerationCost`, `batchPercentileRanks`, `resolveDifficulty`, `paretoPrune`, `optimizeBudgetCandidates`, `costUnitForBudget`; in Python the equivalents (`estimate_generation_cost`, `pareto_prune`, `optimize_budget_candidates`, `build_budget_candidates`) live in `tryaii.budget`.
