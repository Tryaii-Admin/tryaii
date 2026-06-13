# `results.jsonl` — per-prompt rows

One JSON object per line, one line per prompt, in dataset order.

## Common fields (both modes)

| Field | Type | Notes |
|---|---|---|
| `id`, `category`, `prompt` | string | From the [dataset](../dataset/README.md) (or defaults) |
| `bestModel` | string | The selected model |
| `bestScore` | number | The winner's `finalScore` (relative within the row, 0.1–0.95 rescale) |
| `bestReasoning` | string | Scoring explanation, e.g. `Quality: 0.82 on [HumanEval (91%), SWE-bench (74%)] \| Cost efficiency: 0.95 \| Speed: 0.80 (fast)` |
| `topK` | array | `[{ "modelId", "finalScore" }]`, ranked — length per `--top-k` |
| `topBenchmarks` | array | `[{ "name", "score" }]` — the prompt's 5 most similar benchmarks (cosine similarity, 4 dp) |
| `broadCategory`, `subcategory` | string | Router's own classification (independent of your `category` label) |
| `confidence` | number | Top benchmark similarity |
| `routeMs` | number | Routing wall time for this row, ms (2 dp) |

## Priority-mode row

```json
{"id":"q42","category":"code","prompt":"Write a SQL migration","bestModel":"claude-sonnet-4-5-20250929",
 "bestScore":0.95,"bestReasoning":"Quality: 0.89 on [SWE-bench (77%), HumanEval (93%)] | Cost efficiency: 0.82 | Speed: 0.60 (medium)",
 "topK":[{"modelId":"claude-sonnet-4-5-20250929","finalScore":0.95},{"modelId":"gpt-5.1","finalScore":0.87}],
 "topBenchmarks":[{"name":"SWE-bench","score":0.6112},{"name":"HumanEval","score":0.5984}],
 "broadCategory":"TECHNICAL","subcategory":"CODE_TECHNICAL","confidence":0.6112,"routeMs":38.21}
```

### Error rows

In priority mode a failing prompt does not abort the run — its row keeps `id`/`category`/`prompt`, zeroes/empties every routing field, and adds:

```json
{"id":"p7", "...": "...", "bestModel":"", "bestScore":0, "topK":[], "routeMs":12.05,
 "error":"<exception message>"}
```

Filter with `jq 'select(.error)'`. (Budget mode never writes error rows — failures abort the run.)

## Budget-mode rows

All common fields, plus:

| Field | Type | Notes |
|---|---|---|
| `normalBestModel` | string | The quality-best model ignoring the budget |
| `budgetConstrained` | bool | `bestModel !== normalBestModel` — the budget changed this pick |
| `difficulty` | number | Raw difficulty in [0, 1] (4 dp) — see [difficulty](../budget-mode/difficulty.md) |
| `estimatedCost` | number | This row's estimated USD cost (8 dp) |
| `cumulativeCost` | number | Running total through this row (8 dp) |
| `remainingBudget` | number | `maxPrice − cumulativeCost` (8 dp) |
| `inputTokens`, `outputTokens` | int | Token counts used for costing (`outputTokens` reflects the [fit-output](../budget-mode/strict-vs-fit-output.md) reduction when applied) |
| `optimizerStatus` | string | `optimal` \| `infeasible` — same value on every row of the run |

`topK` is the row's full quality ranking trimmed to `--top-k` — reporting only; selection happened in the knapsack.

```json
{"id":"p3","category":"math","prompt":"...","bestModel":"gemini-2.5-flash","normalBestModel":"gpt-5.1",
 "budgetConstrained":true,"bestScore":0.78,"bestReasoning":"...","difficulty":0.1192,
 "estimatedCost":0.00041125,"cumulativeCost":0.00112375,"remainingBudget":0.49887625,
 "inputTokens":58,"outputTokens":2000,"topK":[...],"topBenchmarks":[...],
 "broadCategory":"EDUCATIONAL","subcategory":"MATH","confidence":0.54,"routeMs":35.4,"optimizerStatus":"optimal"}
```

Useful queries:

```bash
jq -c 'select(.budgetConstrained)' results.jsonl          # where the budget changed the pick
jq -s 'map(.estimatedCost) | add' results.jsonl           # total estimated spend
jq -c 'select(.difficulty > 0.5) | {id, bestModel}' results.jsonl
```
