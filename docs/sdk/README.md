# TryAii-DRE SDK

The same routing engine ships as two SDKs — `tryaii` on PyPI and `tryaii` on npm — with matching behavior and naming (Python `snake_case` ↔ Node `camelCase`; e.g. `result.best_model` ↔ `result.bestModel`, `top_k=` ↔ `{ topK }`).

## 30-second quickstart

**Python** (sync):

```python
from tryaii import Router, Priorities

router = Router()
result = router.route("Write a Python function to merge sorted arrays",
                      priorities=Priorities(quality=5, cost=1, speed=2), top_k=3)
print(result.best_model, result.best_reasoning)
```

**Node** (async):

```ts
import { Router, Priorities } from 'tryaii';

const router = new Router();
const result = await router.route('Write a Python function to merge sorted arrays',
  { priorities: new Priorities(5, 1, 2), topK: 3 });
console.log(result.bestModel, result.scores[0]?.reasoning);
```

Routing is fully local — no API key. To also *call* the chosen model, use the [high-level client](client/README.md) or the [OpenRouter integration](client/openrouter.md) with an `OPENROUTER_API_KEY`.

## Capabilities

| Capability | Pages | Key exports |
|---|---|---|
| **Routing** | [routing/](routing/README.md) · [priorities](routing/priorities.md) · [scoring](routing/scoring.md) · [classification](routing/classification.md) | `Router`, `RouteResult`, `Priorities`, `ScoringEngine`, `EmbeddingClassifier` |
| **Client (route + call)** | [client/](client/README.md) · [async](client/async.md) · [openrouter](client/openrouter.md) | `DREClient`, `AsyncDREClient` (Python only), `OpenRouterIntegration` |
| **Budget routing** | [budget/](budget/README.md) | `route_dataset_with_budget` / `routeDatasetWithBudget` |
| **Model catalog** | [models/](models/README.md) · [presets](models/presets.md) | `ModelRegistry`, `ModelInfo`, `ModelPricing` |
| **Benchmarks** | [benchmarks/](benchmarks/README.md) · [centroids](benchmarks/centroids.md) | `BenchmarkRegistry`, `BenchmarkDefinition`, `CentroidLoader`, `CentroidGenerator` |
| **Embeddings** | [embeddings/](embeddings/README.md) | `LocalEmbeddingProvider`, `OpenAIEmbeddingProvider` (Python only), `BaseEmbeddingProvider` |
| **Configuration & caching** | [configuration/](configuration/README.md) · [caching](configuration/caching.md) | `TryaiiDreConfig` / `createDefaultConfig`, `CacheConfig` |
| **Eval dashboard** | [dashboard.md](dashboard.md) | `renderDashboard` (Node export) |

## Import surfaces

- **Python**: everything core is at `tryaii` (`Router`, `RouteResult`, `DREClient`, `AsyncDREClient`, `Priorities`, `ModelRegistry`, `ModelInfo`, `BenchmarkRegistry`, `TryaiiDreConfig`, budget API). Subsystems import from subpackages: `tryaii.classifiers`, `tryaii.embeddings`, `tryaii.centroids`, `tryaii.scoring`, `tryaii.benchmarks`, `tryaii.cache`, `tryaii.integrations`.
- **Node**: everything is exported from the package root `tryaii` (ESM only, Node ≥ 18); the OpenRouter integration is additionally available from the `tryaii/integrations` subpath. There are no deep-import subpaths beyond those two.

## Sync vs async at a glance

| | Python | Node |
|---|---|---|
| `Router.route` | sync | async (`routeSync` exists but requires a sync-capable embedding provider; the default local provider is async-only) |
| High-level client | `DREClient` (sync) + `AsyncDREClient` | `DREClient` (async) |
| Embedding backend | `sentence-transformers` (core dependency) | `@xenova/transformers` (optional dependency, lazy-loaded) |
