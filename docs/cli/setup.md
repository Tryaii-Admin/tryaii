# `tryaii setup` — one-time initialization

Download the embedding model and warm the benchmark centroids so the first real `route`/`eval` is fast. Optional — the same work happens lazily on first use.

```bash
tryaii setup
tryaii setup --model all-mpnet-base-v2
```

## Flags

| Flag | Type | Default | Effect |
|---|---|---|---|
| `--model` | string | `all-MiniLM-L6-v2` | Embedding model name (Python: any `sentence-transformers` model; Node: resolved as `Xenova/<name>` from the HF hub). Python also honors `TRYAII_DRE_EMBEDDING_MODEL`. |

## Behavior

- Downloads the embedding model (cached by the embedding backend) and loads or generates the benchmark centroids of the catalog in use (16 for the starter catalog; more for the full catalog after [`tryaii login`](login.md)).
- For the default model, pre-computed centroids ship inside every catalog (the starter catalog in the package, the full catalog in its download) — setup completes without embedding anything.
- For a non-default model, centroids are generated from the bundled training queries and cached at `~/.tryaii/centroids/centroids_<model>__<kind>-<version>.json`, one file per catalog (`<kind>` is `starter` or `full`; override the directory with `TRYAII_DRE_DATA_DIR` on Python).

```
Setting up TryAii with embedding model: all-MiniLM-L6-v2
This will download the model and load benchmark centroids (one-time operation)...

Setup complete! 16 benchmark centroids ready.
```

To force a rebuild of existing centroids, use [`regenerate`](regenerate.md).
