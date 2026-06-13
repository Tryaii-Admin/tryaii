# Caching

Routing the same (or repeated) prompts is fast because the classifier keeps two in-memory LRU caches with TTL. Both are per-process; nothing routing-related is written to disk except [centroids](../benchmarks/centroids.md).

## CacheConfig

Set via `TryaiiDreConfig.cache` (Python `CacheConfig` dataclass / Node `CacheConfig` interface):

| Field | Default | Caches |
|---|---|---|
| `embedding_cache_size` / `embeddingCacheSize` | 300 | prompt → embedding |
| `classification_cache_size` / `classificationCacheSize` | 150 | prompt → full `ClassificationResult` |
| `ttl_seconds` / `ttlSeconds` | 300 (5 min) | applies to both caches |
| `redis_url` (Python only) | `None` | **Not implemented** — setting it logs a warning and the in-memory LRU is used; the `tryaii[redis]` extra is reserved for a future distributed cache |

```python
config = TryaiiDreConfig(cache=CacheConfig(embedding_cache_size=1000, ttl_seconds=600))
```

```ts
// Reminder: a partial cache object REPLACES the whole block on Node — set all three.
const router = new Router({ config: { cache:
  { embeddingCacheSize: 1000, classificationCacheSize: 500, ttlSeconds: 600 } } });
```

## Cache keys & invalidation

Keys are md5 hashes scoped to the embedding model name + dimension (+ the benchmark-set fingerprint for classification results) + the prompt — identical in both SDKs. Changing the embedding model or the registered benchmark set invalidates naturally; [`router.add_benchmark`](../benchmarks/README.md) also clears the classification cache on a live router. Cache hits are visible as `classification.cache_hit == True`.

## LRUCache

The underlying generic cache is importable for your own use on Python (`from tryaii.cache import LRUCache`; thread-safe, lazy TTL expiry, `get/set/clear/has/size`). The Node equivalent is internal and not exported.
