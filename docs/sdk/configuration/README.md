# Configuration

`TryaiiDreConfig` carries the engine settings: embedding model, data directory, and [cache sizes](caching.md). Pass it (or a partial override) to the `Router`.

## Python

```python
from tryaii import TryaiiDreConfig, Router

config = TryaiiDreConfig(embedding_model="all-mpnet-base-v2",
                         data_dir="~/.tryaii")        # coerced to Path
router = Router(config=config)
```

| Field | Default | Env override |
|---|---|---|
| `embedding_model` | `"all-MiniLM-L6-v2"` | `TRYAII_DRE_EMBEDDING_MODEL` |
| `data_dir` | `~/.tryaii` | `TRYAII_DRE_DATA_DIR` |
| `cache` | `CacheConfig()` (see [caching](caching.md)) | — |
| `strategy` | `"balanced"` | — (currently unused by the router; use [Priorities](../routing/priorities.md)) |
| `openai_api_key` | `None` | `OPENAI_API_KEY` |
| `openrouter_api_key` | `None` | `OPENROUTER_API_KEY` (note: the clients read this env var directly, not via config) |

Precedence: explicit kwarg > environment variable > default. **`import tryaii` loads a `.env` file** from the working directory (`python-dotenv`), so env overrides can live there. Helpers: `config.centroids_dir`, `config.centroid_file`, `config.ensure_dirs()`.

## Node

No config file and no env vars — configuration is purely programmatic:

```ts
import { Router, createDefaultConfig, DEFAULT_DATA_DIR, DEFAULT_EMBEDDING_MODEL } from 'tryaii';

const router = new Router({ config: { embeddingModel: 'all-mpnet-base-v2' } });  // partial merge
const config = createDefaultConfig({ dataDir: '/tmp/tryaii' });
```

`TryaiiDreConfig` fields mirror Python (`embeddingModel`, `dataDir` default `~/.tryaii`, `cache`, `strategy`, `openaiApiKey`/`openrouterApiKey` — the API-key fields are never populated from env here; only `DREClient` falls back to `process.env.OPENROUTER_API_KEY`). `createDefaultConfig(overrides)` does a **shallow** merge — a partial `cache` object replaces the whole cache block.

## Environment variable summary (both SDKs)

| Variable | Python | Node | Used for |
|---|---|---|---|
| `OPENROUTER_API_KEY` | clients + integration fallback | `DREClient` fallback only | Calling models via OpenRouter |
| `OPENAI_API_KEY` | `OpenAIEmbeddingProvider` | — | OpenAI embeddings |
| `TRYAII_DRE_EMBEDDING_MODEL` | ✓ | — | Default embedding model |
| `TRYAII_DRE_DATA_DIR` | ✓ | — | Centroid/cache directory |
| `TRYAII_NO_BANNER` | CLI | CLI | Suppress the [CLI banner](../../cli/README.md#banner) |

## On-disk layout

```
~/.tryaii/
  centroids/
    centroids_<embedding-model>.json    # user centroid cache ("/" in model names becomes "__")
```

Embedding model weights are cached separately by the embedding backend (Hugging Face cache).
