# Default model catalog

`ModelRegistry.default()` loads the models of the **default catalog**. There are two catalogs, and the same engine routes both:

| Catalog | Where it comes from | Size |
|---|---|---|
| **Starter** | Ships inside the package; no network, no account | **45 models from 6 providers, 16 benchmarks** |
| **Full** | Downloaded after [`tryaii login`](../../cli/login.md) (free), refreshed at most once a day, signature-checked before use | **322 routable models** |

With the default `catalog="auto"`, the full catalog is used when you are logged in and the starter catalog otherwise. Pass `catalog="starter"` to always use the packaged catalog (no network), or `catalog="full"` to require the full one (raises `LoginRequiredError` when not logged in):

```python
registry = ModelRegistry.default()                    # catalog="auto"
registry = ModelRegistry.default(catalog="starter")   # packaged, offline
router = Router(catalog="full")
```

```ts
const registry = ModelRegistry.default();                        // 'auto'
const offline = ModelRegistry.default(false, null, 'starter');   // packaged, offline
const router = new Router({ catalog: 'full' });
```

## The starter catalog

| Provider | Models |
|---|---:|
| OpenAI | 16 |
| Anthropic | 10 |
| Google | 8 |
| DeepSeek | 4 |
| xAI | 4 |
| Mistral | 3 |

It covers the well-known frontier and value models of those providers (for example `openai/gpt-5.5`, `anthropic/claude-opus-5.5`, `google/gemini-3.1-pro-preview`, `deepseek/deepseek-v4-pro`, `x-ai/grok-4.3`, `mistralai/mistral-large-2512`), scored on the [16 starter benchmarks](../benchmarks/README.md).

Per model the catalog records: `benchmark_scores` (a subset of the catalog's benchmarks), `pricing` (USD per 1k tokens, input/output), `latency` tier, `tokens_per_second` and `ttft_ms` (when measured), `capabilities` tags, and a description.

## Free-tier variants

Ephemeral OpenRouter `:free` variants come and go within days, so they are never routed, listed or counted. They are opt-in at the registry layer:

```python
registry = ModelRegistry.default(include_free=True)
registry.load_preset("default", include_free=True)
```

```ts
const registry = ModelRegistry.default(true);
registry.loadPreset('default', { includeFree: true });
```

## Inspect it

```bash
tryaii models --json          # the catalog in use, as JSON (:free variants excluded)
tryaii models --provider x-ai # one provider
```

Model IDs are OpenRouter-native slugs such as `openai/gpt-5.5`, so they pass directly to the [OpenRouter integration](../client/openrouter.md). `MODEL_ID_TO_OPENROUTER` remains as a compatibility map for legacy IDs.

Both catalogs are snapshots, not a live runtime feed: the starter catalog changes with package releases, the full catalog with each published catalog release. Scores and prices can age between releases; use a [custom registry](README.md#using-a-custom-registry) or `registry.add(...)` overrides when you need different data.
