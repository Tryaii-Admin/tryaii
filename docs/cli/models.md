# `tryaii models` — list the model catalog

Print every model in the default registry (39 models across OpenAI, Anthropic, Google, DeepSeek, xAI, Mistral — see [SDK presets](../sdk/models/presets.md)).

```bash
tryaii models
tryaii models --provider anthropic
tryaii models --json
```

## Flags

| Flag | Type | Default | Effect |
|---|---|---|---|
| `--provider` | string | — | Case-insensitive exact match on the provider name |
| `--json` | boolean | off | Print the (filtered) models as pretty-printed JSON (`to_dict()` shape: `model_id`, `provider`, `benchmark_scores`, `capabilities`, `pricing {input_per_1k, output_per_1k}`, `latency`, `description`) |

## Text output

Grouped by provider (alphabetical):

```
Available Models (39):
----------------------------------------------------------------------

  Anthropic (7 models):
    - claude-opus-4-5-20251101 [medium] | $0.0050/0.0250
    - ...
```

Each line shows `model_id [latency-tier] | $input/output per 1k tokens` (price segment omitted when the model has no pricing).

Tip: the banner goes to stderr, so `tryaii models --json | jq` works without `--no-banner`.
