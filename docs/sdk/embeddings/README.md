# Embedding providers

Embeddings power [classification](../routing/classification.md) and [centroids](../benchmarks/centroids.md). Two implementations ship; the provider is an open extension point.

Imports — Node: `BaseEmbeddingProvider`, `LocalEmbeddingProvider` from the package root. Python: `from tryaii.embeddings import BaseEmbeddingProvider, LocalEmbeddingProvider, OnnxEmbeddingProvider, SentenceTransformerEmbeddingProvider, OpenAIEmbeddingProvider`.

## LocalEmbeddingProvider (default)

On-device, no API key. Python: the model file is installed with the package (`tryaii-minilm-int8`). Node: `LocalEmbeddingProvider` downloads its model on first embed; `OnnxEmbeddingProvider` (what the monitor uses) runs the same int8 file as Python from the npm package `tryaii-minilm-int8`.

| | Python | Node |
|---|---|---|
| Backend | `onnxruntime` + `tokenizers` running an int8 ONNX file (core dependencies, no PyTorch); any non-ONNX model name runs on `sentence-transformers` (`pip install "tryaii[centroids]"`) | `@xenova/transformers` (ONNX, **optional** dependency — install it explicitly) |
| Constructor | `LocalEmbeddingProvider(model_name="all-MiniLM-L6-v2-onnx-int8", device=None)` (`device` for sentence-transformers only: `"cpu"|"cuda"|"mps"` or auto) | `new LocalEmbeddingProvider('Xenova/all-MiniLM-L6-v2')` (HF org prefix required; `modelName` getter strips it for centroid-file compatibility) |
| Sync support | yes (`embed`, `embed_batch`) | async-only (`embedAsync`, `embedBatchAsync`); `init()` pre-warms |

Default model — Python: `all-MiniLM-L6-v2-onnx-int8` (all-MiniLM-L6-v2 exported to ONNX with weight-only int8 weights and fp32 arithmetic, 23 MB, max 256 tokens, mean pooling, L2-normalized, 384 dimensions; cosine 0.999 or better to the fp32 model, the same vectors on every CPU). Node: `all-MiniLM-L6-v2`. The int8 vectors are their own space: they are scored only against centroids built with the same file, which every catalog publishes next to the fp32 ones (`centroids.<model id>.json`).

The Python model file is `TRYAII_EMBEDDING_MODEL_PATH` when set (a file, or a directory holding `model.onnx`), else the one in the installed `tryaii-minilm-int8` package, which `tryaii` depends on. Nothing is downloaded at run time. Any file must match the size and sha256 pinned in `tryaii.embeddings.model_store.MODELS`. Without one, embedding raises `ModelUnavailableError`; the monitor wrapper fails open instead ([monitor](../monitor.md#the-embedding-model)). `scripts/export-onnx-embedding.py` rebuilds the file reproducibly.

Otherwise: normalized embeddings. Changing the model triggers one-time [centroid regeneration](../benchmarks/centroids.md), cached under `~/.tryaii/centroids/`.

## OnnxEmbeddingProvider (Node, TS-253)

`new OnnxEmbeddingProvider()` runs `all-MiniLM-L6-v2-onnx-int8`, the Python
default, on `onnxruntime-node`. It uses the same WordPiece tokenizer (a
TypeScript port, frozen against Python by `shared/embeddings/tokenizer_parity.json`),
truncation to 256 tokens, mean pooling and L2 normalisation. It is async-only.
It needs the optional peers `onnxruntime-node` and `tryaii-minilm-int8`, or
`TRYAII_EMBEDDING_MODEL_PATH`; without them it throws `ModelUnavailableError`.
The monitor uses it in-process; `Router` still defaults to `LocalEmbeddingProvider`.

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
