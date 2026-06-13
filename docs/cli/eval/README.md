# `tryaii eval` — route a dataset

Route every prompt in a JSON file and write three artifacts: per-prompt results, an aggregate summary, and a self-contained HTML dashboard. Runs locally — no model APIs are called.

```bash
tryaii eval prompts.json --output results/run --quality=5 --cost=1 --speed=1
tryaii eval prompts.json --max-price=0.50 --output-tokens=2000
```

## Choosing a mode

| | [Priority mode](priority-mode/README.md) (default) | [Budget mode](budget-mode/README.md) (`--max-price`) |
|---|---|---|
| Question answered | "Which model is best for each prompt, given my quality/cost/speed weights?" | "Which model should answer each prompt so total quality is maximized under $X for the whole dataset?" |
| Prompts are | routed independently | optimized jointly (shared knapsack) |
| `--quality/--cost/--speed` | drive the ranking | **ignored** (objective is fixed) |
| Per-prompt errors | caught, recorded per row, run continues | not caught — abort the run |

## Documentation map

| Page | Covers |
|---|---|
| [`dataset/`](dataset/README.md) | Input file format: strings vs objects, `id`/`category` rules, encoding, validation errors |
| [`priority-mode/`](priority-mode/README.md) | The default mode: priority flags, error handling, progress |
| [`budget-mode/`](budget-mode/README.md) | Budget optimization: candidates, knapsack, feasibility |
| [`budget-mode/strict-vs-fit-output.md`](budget-mode/strict-vs-fit-output.md) | `--budget-mode` deep dive |
| [`budget-mode/difficulty.md`](budget-mode/difficulty.md) | `--difficulty-source` and `--difficulty-gamma` deep dive |
| [`outputs/`](outputs/README.md) | Artifacts overview and console output |
| [`outputs/results-jsonl.md`](outputs/results-jsonl.md) | Per-prompt row schemas (both modes) |
| [`outputs/summary-json.md`](outputs/summary-json.md) | Aggregate summary schema (incl. the `budget` block) |
| [`outputs/dashboard.md`](outputs/dashboard.md) | The `index.html` dashboard |

## Full flag index

| Flag | Type | Default | Mode | Details |
|---|---|---|---|---|
| `<input.json>` | path | required | both | [dataset/](dataset/README.md) |
| `-o`, `--output` | dir | `./tryaii-eval-<YYYYMMDD-HHMMSS>` | both | Created recursively; [outputs/](outputs/README.md) |
| `--quality` / `--cost` / `--speed` | int | `3` | priority | 1–5, silently clamped; [priority-mode/](priority-mode/README.md) |
| `--top-k` | int | `5` | both | Models recorded per row (budget mode: trims reporting only) |
| `--max-price` | float USD | — | enables budget | [budget-mode/](budget-mode/README.md) |
| `--output-tokens` | int | `1000` | budget | Assumed output length per prompt for costing |
| `--budget-mode` | `strict` \| `fit-output` | `strict` | budget | [strict-vs-fit-output](budget-mode/strict-vs-fit-output.md) |
| `--difficulty-source` | `intrinsic` \| `capability` \| `blend` | `intrinsic` | budget | [difficulty](budget-mode/difficulty.md) |
| `--difficulty-gamma` | float ≥ 0 | `1` | budget | [difficulty](budget-mode/difficulty.md) |

Budget-only flags are accepted and validated even without `--max-price`, but take effect only in budget mode.

## Run lifecycle (both modes)

1. Print `[eval]` status lines: input path, output dir, priorities (or the budget objective), prompt count.
2. **Warm up** the router with one throwaway route — model download/centroid load happens here, so per-prompt timings stay honest. A warmup failure aborts (exit 1).
3. Route (priority mode) or build candidates + optimize (budget mode), printing progress at every ~10%.
4. Write `results.jsonl`, `summary.json`, `index.html` into the output dir (always written, even with partial failures).
5. Print the summary block, top-10 recommended models, and the three artifact paths.

## Exit codes

- `0` — success, **including partial per-prompt failures** (check `errorCount` in `summary.json` in CI).
- `1` — bad input file, warmup failure, budget-mode routing failure, or *every* prompt failed (`[eval] error: all N prompt(s) failed: ...` on stderr).
- `2` — usage error (bad flag value, missing input argument).
