# Model catalog

`ModelRegistry` holds the models the router chooses between; `ModelInfo` describes one model. Both are exported from the package roots (`ModelPricing` too on Node; Python: `from tryaii.registry import ModelPricing`).

## ModelInfo

| Field (Python / Node) | Type | Notes |
|---|---|---|
| `model_id` / `modelId` | str | Registry key |
| `provider` | str | e.g. `OpenAI`, `Anthropic` |
| `benchmark_scores` / `benchmarkScores` | dict | Raw scores keyed by benchmark name (see [benchmarks](../benchmarks/README.md)) |
| `capabilities` | list[str] | Free-form tags (`vision`, `multimodal`, …) |
| `pricing` | `ModelPricing` or None | `input_per_1k` / `output_per_1k` USD; `average_per_1k` property |
| `latency` | tier or None | `very fast | fast | medium | slow | very slow` |
| `description` | str | |

`to_dict()` / `toDict()` and `from_dict()` / `fromDict()` round-trip the snake_case JSON shape (null benchmark scores are dropped on load).

## ModelRegistry

```python
from tryaii import ModelRegistry
registry = ModelRegistry.default()        # the default catalog: 45-model starter, or the full catalog after `tryaii login`
registry = ModelRegistry()                # or start empty and add your own
registry.add("gpt-4o-mini", provider="OpenAI",
             benchmarks={"LiveCodeBench": 62.0, "MMLU-Pro": 76.0},
             pricing=(0.00015, 0.0006),   # (input_per_1k, output_per_1k) USD
             latency="very fast", capabilities=["fast-response"])
```

```ts
import { ModelRegistry } from 'tryaii';
const registry = ModelRegistry.default();
registry.add({ modelId: 'gpt-4o-mini', provider: 'OpenAI',
               benchmarks: { LiveCodeBench: 62.0 }, pricing: [0.00015, 0.0006],
               latency: 'very fast', capabilities: ['fast-response'] });
```

| Method | Notes |
|---|---|
| `add(...)` / `add_model(model)` · `addModel(model)` | Add or replace by model ID |
| `remove_model(id)` / `removeModel(id)` → bool | |
| `get_model(id)` / `getModel(id)` | `None`/`undefined` when absent |
| `filter(provider?, capability?, max_input_cost?, latency?)` | provider: case-insensitive equality; capability: exact membership; `max_input_cost`/`maxInputCost`: `input_per_1k ≤` (unpriced models excluded); latency: exact tier |
| `all_models` / `allModels` · `model_ids` / `modelIds` | Properties |
| `len(registry)` / `registry.length` · `in` / `has(id)` | |
| `load_preset(name="default", include_free=False)` / `loadPreset(name, { includeFree: false })` | Loads the default catalog's models (kept for backwards compatibility; only `default` exists). The opt-in flag includes ephemeral `:free` variants. See [default catalog](presets.md). |
| `export_json(path)` (Python, atomic write) / `exportJson()` (Node, returns the object) | Serialize the registry |

## Using a custom registry

Pass it to the Router — routing then only considers your models:

```python
router = Router(registry=registry)
```

```ts
const router = new Router({ registry });
```

To make custom models *callable* through OpenRouter, their IDs must be valid OpenRouter slugs or appear in `MODEL_ID_TO_OPENROUTER` — see [openrouter](../client/openrouter.md). For meaningful quality differentiation, give custom models scores on the [registered benchmarks](../benchmarks/README.md); missing relevant scores are otherwise imputed from the model's known level and registry medians.
