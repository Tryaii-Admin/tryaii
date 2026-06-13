# Priority mode (default)

Without `--max-price`, every prompt is routed **independently** with your quality/cost/speed priorities — exactly like running [`tryaii route`](../../route.md) per prompt, but batched, timed, and written to [artifacts](../outputs/README.md).

```bash
tryaii eval prompts.json                                  # balanced 3/3/3
tryaii eval prompts.json --quality=5 --cost=1 --speed=1   # quality-first
tryaii eval prompts.json --cost=5 --top-k=3 -o runs/cheap
```

## Flags

| Flag | Default | Effect |
|---|---|---|
| `--quality` / `--cost` / `--speed` | `3` | Priority weights 1–5. Must be integers; out-of-range values are silently clamped to 1–5. See [how priorities become weights](../../../sdk/routing/priorities.md). |
| `--top-k` | `5` | How many ranked models are recorded per row (`topK` field) |
| `-o`, `--output` | timestamped dir | Artifact directory |

## Behavior

- One warmup route runs first so the per-row `routeMs` timings exclude model download/centroid loading.
- Progress prints at every ~10% threshold: `[eval] routed 12/120 (10%)`.
- **Per-prompt errors are caught**: a failing prompt becomes a row with empty routing fields plus an `error` message ([row schema](../outputs/results-jsonl.md#error-rows)), and the run continues. The run only exits 1 if *every* prompt failed.
- Scores within a row are relative (rescaled 0.1–0.95 across that prompt's candidates) — compare models *within* a row, and use the distribution in [`summary.json`](../outputs/summary-json.md) to compare across the dataset.

## Console output

```
[eval] input      : C:\work\prompts.json
[eval] output     : C:\work\runs\cheap
[eval] priorities : quality=2 cost=5 speed=3
[eval] loaded 120 prompt(s)
[eval] warming up router...
[eval] routed 12/120 (10%)
...
[eval] === Summary ===
Prompts        : 120
Successes      : 120
Errors         : 0
Distinct models: 6
Avg route time : 38.4 ms

Top recommended models:
  gemini-2.5-flash-lite                       64  (53.33%)
  ...
[eval] per-prompt results -> ...\results.jsonl
[eval] summary            -> ...\summary.json
[eval] dashboard          -> ...\index.html
```

## When to use which priorities

The four [presets](../../../sdk/routing/priorities.md) map to flag combos: balanced `3/3/3` (default), performance `5/1/1`, budget `2/5/3`, fast `2/3/5`. If what you actually want is "best quality under a total spend", use [budget mode](../budget-mode/README.md) instead of cranking `--cost`.
