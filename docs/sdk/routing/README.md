# Router

`Router` is the core engine: it classifies a prompt against benchmark centroids ([classification](classification.md)), scores every model in the registry ([scoring](scoring.md)) under your [priorities](priorities.md), and returns a ranked `RouteResult`. Local-only — never calls a model API.

## Constructing

All dependencies are injectable; defaults give you the bundled 39-model catalog, 12 standard benchmarks, and local embeddings (initialized lazily on first route).

```python
from tryaii import Router, TryaiiDreConfig
router = Router()                                     # all defaults
router = Router(config=TryaiiDreConfig(embedding_model="all-mpnet-base-v2"),
                registry=my_registry,                 # ModelRegistry
                benchmark_registry=my_benchmarks,     # BenchmarkRegistry
                embedding_provider=my_provider)       # BaseEmbeddingProvider
```

```ts
import { Router } from 'tryaii';
const router = new Router();                          // all defaults
const custom = new Router({
  config: { embeddingModel: 'all-mpnet-base-v2' },    // Partial<TryaiiDreConfig>
  registry, benchmarkRegistry, embeddingProvider,
});
```

## Routing

```python
result = router.route(prompt,
    priorities=Priorities(quality=5, cost=1, speed=2),  # default 3/3/3
    top_k=5,
    filter_provider="Anthropic",     # case-insensitive provider equality
    filter_capability="vision",      # exact membership in model.capabilities
    filter_max_cost=0.001)           # pricing.input_per_1k <= value (unpriced models excluded)
```

```ts
const result = await router.route(prompt, {
  priorities, topK: 5,
  filterProvider: 'anthropic', filterCapability: 'vision', filterMaxCost: 0.001,
});
```

Behavior to know:

- Empty / non-string prompt → `ValueError` / `Error('prompt must be a non-empty string')`.
- Prompts longer than 100,000 chars are silently truncated.
- If filters eliminate every model, `route` does **not** throw — it returns `best_model == ""` with empty `scores` (the high-level clients convert this into an error).
- Node `routeSync()` exists but throws with the default embedding provider (async-only); inject a sync-capable provider to use it. The sync path also can't compute intrinsic difficulty.

## RouteResult

| Python | Node | Meaning |
|---|---|---|
| `best_model` | `bestModel` | Top model ID (`""` when no models survive) |
| `scores` | `scores` | Ranked `ModelScore[]` (see [scoring](scoring.md)) |
| `classification` | `classification` | `ClassificationResult` or `None` (see [classification](classification.md)) |
| `priorities` | `priorities` | The priorities used |
| `best_score` / `best_reasoning` / `top_k` (properties) | `routeResultBestScore(r)` / `routeResultBestReasoning(r)` / `routeResultTopK(r)` (helper functions) | Convenience accessors |

## Extending a live router

```python
router.add_model("my-model", provider="custom",
                 benchmarks={"HumanEval": 85}, pricing=(0.001, 0.002), latency="fast")
router.add_benchmark("CustomerSupportQA", queries=[...10-20 prompts...],
                     description="...", min_score=0, max_score=100)
```

```ts
router.addModel({ modelId: 'my-model', provider: 'custom',
                  benchmarks: { HumanEval: 85 }, pricing: [0.001, 0.002], latency: 'fast' });
await router.addBenchmark('CustomerSupportQA', queries, 'description', 0, 100);
```

`add_benchmark` registers the definition, rebuilds the normalizer, and generates the centroid immediately — subsequent routes see it without restart. Custom-benchmark centroids do **not** persist across processes (the cache file is invalidated by a benchmark-set fingerprint check); re-add them on startup. See [benchmarks](../benchmarks/README.md).

Accessors: `router.models` ([ModelRegistry](../models/README.md)), `router.benchmarks` ([BenchmarkRegistry](../benchmarks/README.md)), `router.config` ([configuration](../configuration/README.md)).
