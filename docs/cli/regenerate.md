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

Embeds the catalog's training queries with the chosen model and writes the centroid file. The starter catalog has 232 training queries across 16 benchmarks; after [`tryaii login`](login.md) the full catalog's queries are used instead.

```
Regenerating centroids for: all-MiniLM-L6-v2
Done! Generated 16 centroids at ~/.tryaii/centroids/centroids_all-MiniLM-L6-v2__starter-2026.10.07.1.json
```

The cache path is `<data-dir>/centroids/centroids_<model with "/" → "__">__<kind>-<version>.json`, one file per catalog (`<kind>` is `starter` or `full`, `<version>` the catalog release) (data dir defaults to `~/.tryaii`; Python honors `TRYAII_DRE_DATA_DIR`).

Note: custom benchmarks added at runtime via the SDK ([`router.add_benchmark`](../sdk/benchmarks/README.md)) are not part of the bundled query set, so they are not included by `regenerate`.
