# `tryaii route` — route one prompt

Classify a prompt with local embeddings and print the top-K model recommendations. No API key needed; nothing is called.

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

Scores are relative per call (rescaled into 0.1–0.95 across the candidate set) — don't compare them across different prompts.

## Exit codes

0 success · 1 routing/embedding failure · 2 missing prompt or bad flag value.
