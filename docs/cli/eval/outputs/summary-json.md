# `summary.json` — run aggregates

Pretty-printed JSON describing the whole run. This exact shape also feeds the [dashboard](dashboard.md) and Node's exported [`renderDashboard`](../../../sdk/dashboard.md).

## Top-level shape

```json
{
  "totalPrompts": 120,
  "successCount": 118,
  "errorCount": 2,
  "distinctModels": 7,
  "avgRouteMs": 41.2,
  "totalRouteMs": 4944.0,
  "priorities": { "quality": 5, "cost": 1, "speed": 1 },
  "distribution": [
    { "model": "claude-sonnet-4-5-20250929", "count": 60, "pct": 50.85 }
  ],
  "byCategory": [
    {
      "category": "code",
      "count": 80,
      "topModels": [ { "model": "...", "count": 40, "pct": 50.0 } ],
      "topBenchmarks": [ { "name": "HumanEval", "avgScore": 0.6021 } ]
    }
  ]
}
```

| Field | Notes |
|---|---|
| `successCount` / `errorCount` | Rows without/with an `error` field. **CI should assert `errorCount == 0`** — the process exits 0 on partial failures. |
| `avgRouteMs` / `totalRouteMs` | Over all rows including errored ones (2 dp) |
| `priorities` | The flags as given — in budget mode they were ignored for selection (see the `budget` block) |
| `distribution` | `bestModel` counts over **successful** rows, sorted count desc (ties keep first-seen order — identical across Node and Python) |
| `byCategory` | Grouped by the dataset's `category` label, sorted by count desc. `topModels` mirrors `distribution` within the category; `topBenchmarks` is the top 5 by average similarity (4 dp). |

## The `budget` block

Budget runs add one more key:

```json
"budget": {
  "status": "optimal",
  "budget": 0.5,
  "budgetMode": "fit-output",
  "difficultySource": "intrinsic",
  "selectionObjective": "maximizeQualityUnderBudget",
  "prioritiesIgnored": true,
  "requestedOutputTokens": 2000,
  "effectiveOutputTokens": 731,
  "outputTokens": 731,
  "totalEstimatedCost": 0.49912345,
  "minimumRequiredBudget": 0.31200000,
  "requestedMinimumRequiredBudget": 0.85300000,
  "budgetShortfall": 0.35300000,
  "costUnit": 0.00005,
  "message": "Requested output tokens did not fit the budget; optimized with 731 output tokens per prompt."
}
```

| Field | Notes |
|---|---|
| `status` | `optimal` \| `infeasible` — see [feasibility outcomes](../budget-mode/README.md#feasibility-outcomes-optimizerstatus) |
| `requestedOutputTokens` / `effectiveOutputTokens` | Differ only when [fit-output](../budget-mode/strict-vs-fit-output.md) shrank answers; `outputTokens` duplicates the effective value |
| `totalEstimatedCost` | Sum of the selected candidates' estimated costs (8 dp) |
| `minimumRequiredBudget` | Cheapest possible total at the *effective* output tokens; `null` when some prompt had no priced candidates |
| `requestedMinimumRequiredBudget` | Cheapest total at the *requested* output tokens (`null` when n/a) |
| `budgetShortfall` | `max(0, requestedMinimumRequiredBudget − budget)` — how much more budget the original request needed; `null` when unpriceable |
| `costUnit` | The knapsack's cost discretization unit (`budget / 10000`) |
| `message` | Human-readable note for infeasible/fitted runs; absent or empty otherwise |
