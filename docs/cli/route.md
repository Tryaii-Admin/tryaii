# `tryaii route` — route one prompt

Classify a prompt with local embeddings and print the top-K model recommendations. No API key needed; no model is called.

```bash
tryaii route "Write a Python function to merge sorted arrays" --quality=5 --cost=1
```

## Arguments & flags

| | Type | Default | Notes |
|---|---|---|---|
| `<prompt>` | string | required | Missing prompt → exit 2 |
| `--quality` | int | `3` | Quality priority 1–5 |
| `--cost` | int | `3` | Cost priority 1–5 |
| `--speed` | int | `3` | Speed priority 1–5 |
| `--top-k` | int | `5` | Number of recommendations shown |

Priority values must be integers (non-integers → exit 2 on Node), but out-of-range values are **silently clamped** to 1–5, not rejected (`--quality=99` behaves as 5).

## Output

Human-readable text on stdout (there is no `--json` mode for `route` — use the SDK or `eval` for machine-readable output):

```
Prompt: <prompt>
Category: <broadCategory> > <subcategory>
Confidence: 0.612
Classifier: embedding

Top 5 Recommendations:
----------------------------------------------------------------------
  1. <modelId>
     Provider: <provider> | Score: 0.950
     Quality: 0.812 | Cost: 0.970 | Speed: 0.800
     Pricing: $0.0030/$0.0150 per 1k        (omitted when the model has no pricing)
     Reason: <scoring reasoning>
```

`Score` is meaningful within one call only: models inside the quality band score in `[0.5, 1.0]`, models outside it below 0.5 (see [scoring](../sdk/routing/scoring.md)) — don't compare it across different prompts. `Quality`, `Cost` and `Speed` are the per-model utilities and are comparable across calls.

`route` uses the catalog in use: the 45-model starter catalog shipped in the package, or the full catalog after [`tryaii login`](login.md). When nobody is logged in, a one-line login hint is printed to stderr at most once a day (`TRYAII_NO_BANNER` silences it).

## Exit codes

0 success · 1 routing/embedding failure · 2 missing prompt or bad flag value.
