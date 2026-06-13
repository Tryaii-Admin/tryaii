# Budget mode (`--max-price`)

Passing `--max-price` switches `eval` to **dataset-wide budget optimization**: pick one model per prompt so that *total quality across the dataset* is maximized while the *estimated total cost* stays under the budget. The whole dataset is solved jointly — cheap models on easy prompts buy headroom for strong models on hard ones.

```bash
tryaii eval prompts.json --max-price=0.50 --output-tokens=2000
tryaii eval prompts.json --max-price=0.10 --output-tokens=2000 --budget-mode=fit-output
tryaii eval prompts.json --max-price=0.50 --difficulty-source=capability --difficulty-gamma=3
```

## Flags

| Flag | Type | Default | Details |
|---|---|---|---|
| `--max-price` | float USD ≥ 0 | — | Total budget for the dataset. Presence enables this mode. |
| `--output-tokens` | int ≥ 0 | `1000` | Assumed output length per prompt for costing (input side is estimated at ~4 chars/token from the prompt itself) |
| `--budget-mode` | `strict` \| `fit-output` | `strict` | What to do when the budget can't cover the requested output length — [strict-vs-fit-output.md](strict-vs-fit-output.md) |
| `--difficulty-source` | `intrinsic` \| `capability` \| `blend` | `intrinsic` | Which difficulty signal shifts budget toward hard prompts — [difficulty.md](difficulty.md) |
| `--difficulty-gamma` | float ≥ 0 | `1` | How strongly difficulty shifts budget (`0` disables) — [difficulty.md](difficulty.md) |

**`--quality/--cost/--speed` are ignored** — the objective is fixed (`selectionObjective: "maximizeQualityUnderBudget"`, `prioritiesIgnored: true` in the summary). `--top-k` only trims the reported `topK` list per row; it never affects selection.

## How the optimizer works

1. **Candidates** — each prompt is routed once against *all* models (internally with quality-max priorities 5/1/1; cost is the knapsack's job, not the score's). Every model becomes a candidate with `estimatedCost = (inputTokens/1000)·input_per_1k + (outputTokens/1000)·output_per_1k`. Models without pricing are dropped. Progress prints as `[eval] built candidates N/M (...%)`.
2. **Difficulty weighting** — each prompt gets a difficulty in [0, 1] (per `--difficulty-source`), the difficulties are percentile-ranked across the dataset, and every candidate's utility (its quality score) is multiplied by `1 + gamma × rank`. See [difficulty.md](difficulty.md).
3. **Pruning** — per prompt, candidates that are more expensive but no better in quality are Pareto-pruned.
4. **Knapsack** — a multiple-choice dynamic program picks exactly one candidate per prompt under the budget (discretized into ~10,000 cost units, so resolution scales with any budget size). Ties prefer cheaper totals.

## Feasibility outcomes (`optimizerStatus`)

| Situation | Result |
|---|---|
| A selection fits the budget | `optimal` — the utility-maximal assignment |
| Some prompt has **no priced candidates** | `infeasible`, empty selection → `results.jsonl` has **zero rows**, `minimumRequiredBudget: null` (was infinite) |
| Cheapest-possible assignment still exceeds the budget | `infeasible`, but the cheapest-per-prompt assignment **is still selected and written**, with `minimumRequiredBudget` / `budgetShortfall` telling you how much budget you'd need. In `fit-output` mode this triggers the output-token search instead — [strict-vs-fit-output.md](strict-vs-fit-output.md). |
| DP quantization artifact (rare) | Falls back to the cheapest assignment, reported as `optimal` (float feasibility was already proven) |

## Behavior differences vs. priority mode

- Per-prompt routing errors are **not** caught — any failure aborts the run (exit 1).
- Each row gains budget fields: `normalBestModel` (the unconstrained quality-best pick), `budgetConstrained` (true when the budget changed the pick), `difficulty`, `estimatedCost`, `cumulativeCost`, `remainingBudget`, token counts, `optimizerStatus` — full schema in [results-jsonl.md](../outputs/results-jsonl.md#budget-mode-rows).
- `summary.json` gains a `budget` block — [summary-json.md](../outputs/summary-json.md#the-budget-block).
- Console adds `[eval] budget : $0.500000 total, 2000 output tokens/prompt, mode=strict, difficulty=intrinsic` and `[eval] optimizer status: optimal`.

Costs are **estimates** (deterministic 4-chars≈1-token input estimate × preset prices) — treat the budget as a planning constraint, not a billing guarantee.

The same engine is available programmatically — [SDK budget routing](../../../sdk/budget/README.md).
