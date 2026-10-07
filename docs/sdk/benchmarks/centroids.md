# Centroids

A centroid is the L2-normalized mean embedding of a benchmark's training queries — the semantic "fingerprint" the classifier compares prompts against. The SDKs manage them automatically; these APIs exist for custom embedding models, custom benchmarks, and cache control.

Imports — Node: `CentroidGenerator`, `CentroidLoader` from the package root. Python: `from tryaii.centroids import CentroidGenerator, CentroidLoader`.

## Where centroids come from (load order)

1. In-memory (already loaded this process).
2. User cache file: `<data-dir>/centroids/centroids_<model with "/" → "__">__<kind>-<version>.json` (default `~/.tryaii/...`), one file per catalog (`<kind>` is `starter` or `full`), so switching between the starter and the full catalog never overwrites the other's cache.
3. The catalog's own centroids — every catalog (the starter catalog in the package, the full catalog in its download) ships them pre-computed for the default `all-MiniLM-L6-v2` model, so first run needs no embedding work.
4. Freshly generated from the catalog's training queries (232 queries across 16 benchmarks in the starter catalog), then saved to the user cache.

A cached/bundled file is rejected (and regenerated) when its embedding-model name, dimension, or **benchmark-set fingerprint** (sorted names joined by `|`) doesn't match — which is why [custom benchmark](README.md#adding-a-custom-benchmark) centroids don't survive restarts.

## CentroidLoader

```python
loader = CentroidLoader(config=TryaiiDreConfig(), embedding_provider=provider)
centroids = loader.get_centroids()                  # load-or-generate per the order above
loader.regenerate()                                 # force rebuild (what `tryaii regenerate` calls)
loader.add_benchmark_centroid("MyBench", queries)   # extend the live set + persist to user cache
loader.remove_benchmark("MyBench")
loader.available_benchmarks
```

```ts
const loader = new CentroidLoader(provider, userCachePath /* optional; no disk cache when omitted */);
await loader.getCentroidsAsync();        // use the async variants with the default provider
await loader.addBenchmarkCentroidAsync('MyBench', queries);
loader.removeBenchmark('MyBench');
```

Node constructor note: the Node loader takes `(embeddingProvider, userCachePath?)` while Python takes `(config, embedding_provider)`. The Node sync methods (`getCentroids`, `regenerate`, `addBenchmarkCentroid`, `availableBenchmarks`) throw if generation is needed with the default async-only provider.

## CentroidGenerator

```python
gen = CentroidGenerator(embedding_provider)
centroids = gen.generate()                          # bundled queries; or pass {name: [queries]}
vec = gen.generate_from_custom("MyBench", queries)  # one centroid
gen.save(centroids, path)                           # JSON {metadata, centroids}
centroids, metadata = CentroidGenerator.load(path)
```

Node mirrors this (`generate`/`generateAsync`, `generateFromCustom[Async]`, `save`, static `load`).

The saved JSON carries `metadata: { model, dimension, benchmark_count }` used by the validation rules above. Centroid math is identical across SDKs, so files are interchangeable for the same embedding model.
