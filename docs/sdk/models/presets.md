# Default model preset

`ModelRegistry.default()` loads the single bundled preset (`default`): **33 models** with curated benchmark scores, pricing, latency tiers, capabilities, and descriptions (preset version `0.1.0`, data snapshot `2026-01`).

| Provider | Count | Model IDs |
|---|---|---|
| OpenAI | 12 | `gpt-4o`, `gpt-4o-mini`, `o1`, `o3`, `o4-mini`, `gpt-5`, `gpt-5-mini`, `gpt-5-nano`, `gpt-5.1`, `gpt-5.2`, `gpt-4.1`, `gpt-4.1-nano` |
| Google | 6 | `gemini-2.5-pro`, `gemini-2.0-flash`, `gemini-2.5-flash`, `gemini-2.5-flash-lite`, `gemini-3-pro-preview`, `gemini-3-flash-preview` |
| xAI | 6 | `grok-3-latest`, `grok-3-mini-latest`, `grok-4-latest`, `grok-4-fast`, `grok-4-1-fast-reasoning-latest`, `grok-code-fast` |
| Anthropic | 5 | `claude-3-7-sonnet-20250219`, `claude-sonnet-4-20250514`, `claude-sonnet-4-5-20250929`, `claude-haiku-4-5-20251001`, `claude-opus-4-5-20251101` |
| DeepSeek | 2 | `deepseek-reasoner`, `deepseek-chat` |
| Mistral | 2 | `mistral-large-latest`, `mistral-small-latest` |

Per model the preset records: `benchmark_scores` (a subset of the [12 standard benchmarks](../benchmarks/README.md)), `pricing` (USD per 1k tokens, input/output), `latency` tier, `capabilities` tags, and a one-line `description`.

Inspect it anytime:

```bash
tryaii models --json          # full preset as JSON
tryaii models --provider xai  # one provider
```

Every preset model also has an entry in `MODEL_ID_TO_OPENROUTER`, so it is directly callable through the [OpenRouter integration](../client/openrouter.md).

The preset is a static snapshot — scores and prices age. To route over current data, [build your own registry](README.md#using-a-custom-registry) or update the scores with `registry.add(...)` overrides.
