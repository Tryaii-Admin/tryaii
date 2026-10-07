# `results.jsonl` — per-prompt rows

One JSON object per line, one line per prompt, in dataset order.

## Common fields (both modes)

| Field | Type | Notes |
|---|---|---|
| `id`, `category`, `prompt` | string | From the [dataset](../dataset/README.md) (or defaults) |
| `bestModel` | string | The selected model |
| `bestScore` | number | The winner's `finalScore` (comparable within the row only: in-band contenders score 0.5–1.0, see [scoring](../../../sdk/routing/scoring.md)) |
| `bestReasoning` | string | Scoring explanation, e.g. `q'=0.89 (4 real of 5) \| imputed: 1/5 \| cost 0.4489 ($2.25/M) \| speed 0.5353 (2.55 s to 300 tok) \| within 0.136 of the best (0.95) \| ...` ([format](../../../sdk/routing/scoring.md#the-reasoning-string)) |
| `topK` | array | `[{ "modelId", "finalScore" }]`, ranked — length per `--top-k` |
| `topBenchmarks` | array | `[{ "name", "score" }]` — the prompt's 5 most similar benchmarks (cosine similarity, 4 dp) |
| `broadCategory`, `subcategory` | string | Router's own classification (independent of your `category` label) |
| `confidence` | number | Top benchmark similarity |
| `routeMs` | number | Routing wall time for this row, ms (2 dp) |

## Priority-mode row

```json
{"id":"p1","category":"coding","prompt":"Fix the off-by-one error in this binary search","bestModel":"google/gemini-3.8-flash",
 "bestScore":0.746,"bestReasoning":"q'=0.89 (4 real of 5) | imputed: 1/5 | cost 0.4489 ($2.25/M) | speed 0.5353 (2.55 s to 300 tok) [ttft estimated] | within 0.136 of the best (0.95) | at 3/3/3 you accept up to 0.136 less quality for a cheaper or faster model; this pick gave up 0.06 vs anthropic/claude-opus-5.5",
 "topK":[{"modelId":"google/gemini-3.8-flash","finalScore":0.746},{"modelId":"openai/gpt-5.5","finalScore":0.6655}],
 "topBenchmarks":[{"name":"Chatbot Arena Elo (Code)","score":0.2659},{"name":"LiveCodeBench","score":0.26}],
 "broadCategory":"TECHNICAL","subcategory":"CODE_TECHNICAL","confidence":0.2659,"routeMs":24.0}
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
{"id":"p3","category":"math","prompt":"...","bestModel":"google/gemini-2.5-flash","normalBestModel":"openai/gpt-5.5",
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
