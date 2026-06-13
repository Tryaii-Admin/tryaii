# `--budget-mode`: strict vs fit-output

Controls what happens when the budget cannot cover every prompt at the requested `--output-tokens`.

## `strict` (default)

The requested output length is non-negotiable. If even the cheapest-per-prompt assignment exceeds the budget, the run reports `infeasible` — but still completes: the cheapest assignment is selected and written, so the artifacts show you the floor.

```bash
tryaii eval prompts.json --max-price=0.05 --output-tokens=2000
```

```
[eval] optimizer status: infeasible
```

In the summary's [`budget` block](../outputs/summary-json.md#the-budget-block): `minimumRequiredBudget` is the cheapest possible total, and `budgetShortfall = minimumRequiredBudget − budget` is exactly how much you're missing. Use `strict` when answer length is a hard product requirement and you'd rather raise the budget than shrink output.

## `fit-output`

Answer length is negotiable; completing the dataset under budget is not. When strict optimization is infeasible (and `--output-tokens` > 0 and at least one priced candidate exists per prompt), the engine **binary-searches the largest output-token count in [1, requested]** whose cheapest assignment fits, re-prices all candidates at that length, and re-runs the optimization.

```bash
tryaii eval prompts.json --max-price=0.10 --output-tokens=2000 --budget-mode=fit-output
```

```
[eval] optimizer status: optimal
[eval] output fit : 2000 -> 731 tokens/prompt
```

Reading the result:

- `requestedOutputTokens` / `effectiveOutputTokens` in the budget block show the reduction; each row's `outputTokens` reflects the effective value.
- `message`: `"Requested output tokens did not fit the budget; optimized with <n> output tokens per prompt."`
- `minimumRequiredBudget` reflects the *fitted* token count; `requestedMinimumRequiredBudget` and `budgetShortfall` still describe the original request, so you can see both "what it now costs" and "what the original ask was short by".
- If even **1** output token per prompt doesn't fit, the original `infeasible` result is returned unchanged (cheapest assignment selected, like strict).

## Choosing

| You want | Use |
|---|---|
| Fixed answer length; fail loudly when budget is short | `strict` |
| Always complete the dataset; shorter answers acceptable | `fit-output` |
| To know the cost of a target length before committing | run `strict` once and read `minimumRequiredBudget` |

Both modes validate eagerly: any value other than `strict`/`fit-output` exits 2, even when `--max-price` is absent.
