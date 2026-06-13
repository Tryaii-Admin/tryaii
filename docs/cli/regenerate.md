# `tryaii regenerate` — rebuild centroids

Force-regenerate the benchmark centroids from the bundled training queries, overwriting the user cache. Use after changing the embedding model or when the centroid cache is suspect — [`setup`](setup.md) only builds what's missing; `regenerate` always rebuilds.

```bash
tryaii regenerate
tryaii regenerate --model all-mpnet-base-v2
```

## Flags

| Flag | Type | Default | Effect |
|---|---|---|---|
| `--model` | string | `all-MiniLM-L6-v2` | Embedding model to generate with (Python also honors `TRYAII_DRE_EMBEDDING_MODEL`) |

## Behavior

Embeds the 12 × 15 bundled training queries with the chosen model and writes the centroid file:

```
Regenerating centroids for: all-MiniLM-L6-v2
Done! Generated 12 centroids at ~/.tryaii/centroids/centroids_all-MiniLM-L6-v2.json
```

The cache path is `<data-dir>/centroids/centroids_<model with "/" → "__">.json` (data dir defaults to `~/.tryaii`; Python honors `TRYAII_DRE_DATA_DIR`).

Note: custom benchmarks added at runtime via the SDK ([`router.add_benchmark`](../sdk/benchmarks/README.md)) are not part of the bundled query set, so they are not included by `regenerate`.
