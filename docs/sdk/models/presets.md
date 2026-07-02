# Default model preset

`ModelRegistry.default()` loads the single bundled preset (`default`): **39 models** with curated benchmark scores, pricing, latency tiers, capabilities, and descriptions (preset version `0.2.0`, data snapshot `2026-06`).

| Provider | Count | Model IDs |
|---|---|---|
| OpenAI | 14 | `gpt-4o`, `gpt-4o-mini`, `o3`, `o4-mini`, `gpt-5`, `gpt-5-mini`, `gpt-5-nano`, `gpt-4.1`, `gpt-5.1`, `gpt-5.2`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.4-nano`, `gpt-5.5` |
| Anthropic | 7 | `claude-sonnet-4-20250514`, `claude-sonnet-4-5-20250929`, `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`, `claude-opus-4-5-20251101`, `claude-opus-4-8`, `claude-fable-5` |
| Google | 7 | `gemini-2.5-pro`, `gemini-2.5-flash`, `gemini-2.5-flash-lite`, `gemini-3-flash-preview`, `gemini-3.5-flash`, `gemini-3.1-pro-preview`, `gemini-3.1-flash-lite` |
| DeepSeek | 4 | `deepseek-reasoner`, `deepseek-chat`, `deepseek-v4-pro`, `deepseek-v4-flash` |
| xAI | 4 | `grok-4-latest`, `grok-4.3`, `grok-4-1-fast-reasoning-latest`, `grok-code-fast` |
| Mistral | 3 | `mistral-large-latest`, `mistral-small-latest`, `mistral-medium-2508` |

Per model the preset records: `benchmark_scores` (a subset of the [12 standard benchmarks](../benchmarks/README.md)), `pricing` (USD per 1k tokens, input/output), `latency` tier, `capabilities` tags, and a one-line `description`.

Inspect it anytime:

```bash
tryaii models --json          # full preset as JSON
tryaii models --provider xai  # one provider
```

Every preset model also has an entry in `MODEL_ID_TO_OPENROUTER`, so it is directly callable through the [OpenRouter integration](../client/openrouter.md).

The preset is a static snapshot — scores and prices age. To route over current data, [build your own registry](README.md#using-a-custom-registry) or update the scores with `registry.add(...)` overrides.
