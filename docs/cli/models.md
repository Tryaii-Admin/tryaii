# `tryaii models` — list the model catalog

Print every routable model in the catalog in use. Out of the box that is the **starter catalog** shipped in the package: 45 well-known models from 6 providers (OpenAI, Anthropic, Google, DeepSeek, xAI, Mistral). After [`tryaii login`](login.md) (free) it lists the **full catalog** of 322 routable models; ephemeral `:free` variants are never listed. See [SDK presets](../sdk/models/presets.md).

When nobody is logged in, `models` (like `route` and `eval`) prints a one-line login hint to **stderr** at most once a day; stdout is unaffected, and `TRYAII_NO_BANNER` silences it:

```
Routing on the starter catalog (45 models). Log in for free to use the full catalog (322 models): tryaii login
```

```bash
tryaii models
tryaii models --provider anthropic
tryaii models --json
```

## Flags

| Flag | Type | Default | Effect |
|---|---|---|---|
| `--provider` | string | — | Case-insensitive exact match on the provider name |
| `--json` | boolean | off | Print the (filtered) models as pretty-printed JSON (`to_dict()` shape: `model_id`, `provider`, `benchmark_scores`, `capabilities`, `pricing {input_per_1k, output_per_1k}`, `latency`, `tokens_per_second` (null when unmeasured), `ttft_ms` (null when unmeasured), `description`) |

## Text output

Grouped by provider (alphabetical):

```
Available Models (45):
----------------------------------------------------------------------

  anthropic (10 models):
    - anthropic/claude-sonnet-4 [fast] | $0.0030/0.0150
    - anthropic/claude-sonnet-4.5 [fast] | $0.0030/0.0150
    - ...
```

Each line shows `model_id [latency-tier] | $input/output per 1k tokens` (price segment omitted when the model has no pricing).

## Speed fields

`tokens_per_second` and `ttft_ms` are the two measured speed signals, both taken from the model's fastest provider. `ttft_ms` is the **median** time to first token over that provider's sampled rows (median, not mean, and only emitted with at least 3 sampled rows — one cold start would otherwise move a model by two minutes). Routing combines them into the seconds-to-a-300-token-answer that the speed utility is defined on:

```
T300 = ttft_ms / 1000 + 300 / tokens_per_second
```

`latency` remains a display bucket only; [scoring](../sdk/routing/scoring.md) never reads it. A model missing `ttft_ms` falls back to the catalog median TTFT; one missing `tokens_per_second` falls back to the catalog p25 speed utility. Both show up as flags in the routing reasoning (`ttft estimated`, `speed unknown (catalog p25)`).

Tip: the banner and the login hint go to stderr, so `tryaii models --json | jq` works without `--no-banner`.
