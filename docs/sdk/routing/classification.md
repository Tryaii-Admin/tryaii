# Classification

Before scoring, the router classifies the prompt: it embeds the text locally and measures cosine similarity to one pre-computed **centroid per benchmark** (see [centroids](../benchmarks/centroids.md)). The similarities drive quality scoring; the top benchmark determines the displayed category.

Imports — Node: `BaseClassifier`, `EmbeddingClassifier`, `ClassificationResult`, `emptyClassificationResult`, `topBenchmarks` from the package root. Python: `from tryaii.classifiers import BaseClassifier, EmbeddingClassifier, ClassificationResult`.

## ClassificationResult

Available on every route as `result.classification`:

| Python | Node | Meaning |
|---|---|---|
| `benchmark_scores` | `benchmarkScores` | Cosine similarity per benchmark, clamped ≥ 0 |
| `broad_category` / `subcategory` | `broadCategory` / `subcategory` | From the top benchmark's category mapping (e.g. `TECHNICAL > CODE_TECHNICAL`) |
| `confidence` | `confidence` | The top similarity |
| `classifier_used` | `classifierUsed` | Currently always `"embedding"` |
| `cache_hit` | `cacheHit` | Whether the result came from the LRU cache |
| `processing_time_ms` | `processingTimeMs` | |
| `difficulty` | `difficulty` | Intrinsic difficulty in [0, 1] (below); 0 when unavailable |

`top_benchmarks` (Python property) / `topBenchmarks(result)` (Node helper) returns the similarities sorted descending.

## Intrinsic difficulty

The classifier also embeds 24 "easy" and 24 "hard" exemplar prompts (identical lists in both SDKs), builds an easy and a hard centroid, and computes `difficulty = 1 / (1 + e^(−10·(simHard − simEasy)))`. This is the `intrinsic` signal used by [budget routing](../budget/README.md). On the Node sync path (`routeSync`) difficulty is not computed.

## EmbeddingClassifier

Built automatically by the Router; construct directly only for custom pipelines:

```python
EmbeddingClassifier(embedding_provider, centroid_loader, config=None)  # cache sizes from config.cache
```

```ts
new EmbeddingClassifier(embeddingProvider, centroidLoader,
  { embeddingCacheSize: 300, classificationCacheSize: 150, ttlSeconds: 300 });
```

Results are cached in two LRUs (embedding + full classification) keyed by embedding model, dimension, benchmark-set fingerprint, and prompt — changing any of those invalidates naturally. See [caching](../configuration/caching.md).

## Custom classifiers

Subclass `BaseClassifier` (`classify(prompt) → ClassificationResult`, `is_ready()`/`isReady()`; Node also has an overridable `classifyAsync`). The abstraction exists for tests and research — the Router currently wires only the embedding classifier, so a custom classifier requires assembling the pipeline yourself.
