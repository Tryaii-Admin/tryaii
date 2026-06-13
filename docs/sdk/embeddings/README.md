# Embedding providers

Embeddings power [classification](../routing/classification.md) and [centroids](../benchmarks/centroids.md). Two implementations ship; the provider is an open extension point.

Imports — Node: `BaseEmbeddingProvider`, `LocalEmbeddingProvider` from the package root. Python: `from tryaii.embeddings import BaseEmbeddingProvider, LocalEmbeddingProvider, OpenAIEmbeddingProvider`.

## LocalEmbeddingProvider (default)

On-device, no API key, lazy model download on first embed.

| | Python | Node |
|---|---|---|
| Backend | `sentence-transformers` (core dependency) | `@xenova/transformers` (ONNX, **optional** dependency — install it explicitly) |
| Constructor | `LocalEmbeddingProvider(model_name="all-MiniLM-L6-v2", device=None)` (`"cpu"|"cuda"|"mps"` or auto) | `new LocalEmbeddingProvider('Xenova/all-MiniLM-L6-v2')` (HF org prefix required; `modelName` getter strips it for centroid-file compatibility) |
| Sync support | yes (`embed`, `embed_batch`) | async-only (`embedAsync`, `embedBatchAsync`); `init()` pre-warms |

Default model: `all-MiniLM-L6-v2`, 384 dimensions, normalized embeddings. Changing the model triggers one-time [centroid regeneration](../benchmarks/centroids.md), cached under `~/.tryaii/centroids/`.

## OpenAIEmbeddingProvider (Python only)

API-based embeddings — `pip install tryaii[openai]`, key from `OPENAI_API_KEY` (or `api_key=`).

```python
from tryaii import Router
from tryaii.embeddings import OpenAIEmbeddingProvider

router = Router(embedding_provider=OpenAIEmbeddingProvider())             # text-embedding-3-small
router = Router(embedding_provider=OpenAIEmbeddingProvider(model="text-embedding-3-large"))
```

Known dimensions: `text-embedding-3-small` 1536, `text-embedding-3-large` 3072, `text-embedding-ada-002` 1536 (unknown models assume 1536). Client uses 30 s timeout, 2 retries. Missing `openai` package raises lazily with an install hint.

## Writing a custom provider

**Python** — implement all four members of `BaseEmbeddingProvider`:

```python
class MyProvider(BaseEmbeddingProvider):
    def embed(self, text: str) -> np.ndarray: ...          # 1-D float array
    def embed_batch(self, texts: list[str]) -> list[np.ndarray]: ...
    @property
    def dimension(self) -> int: ...
    @property
    def model_name(self) -> str: ...
```

**Node** — the async path is mandatory; sync is opt-in:

```ts
class MyProvider extends BaseEmbeddingProvider {
  async embedAsync(text: string): Promise<number[]> { ... }   // override (default wraps sync embed())
  get dimension(): number { ... }
  get modelName(): string { ... }
  // For sync support: override embed() and `get supportsSync() { return true; }`
  // — that also unlocks Router.routeSync() and the sync centroid methods.
}
```

Pass it to the Router (`Router(embedding_provider=...)` / `new Router({ embeddingProvider })`). Centroids are cached per `model_name` + dimension, so distinct providers must report distinct model names.
