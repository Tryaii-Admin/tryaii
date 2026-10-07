# Benchmarks

A *benchmark* is the unit of routing signal: a name, a normalization range, and a set of training queries whose [centroid](centroids.md) the prompt is compared against. Models earn quality from their scores on the benchmarks most similar to the prompt.

Imports — Node: `BenchmarkRegistry`, `BenchmarkDefinition`, `STANDARD_BENCHMARKS` from the package root. Python: `BenchmarkRegistry` from `tryaii`; `BenchmarkDefinition`, `STANDARD_BENCHMARKS` from `tryaii.benchmarks`.

## The standard benchmarks

`BenchmarkRegistry.default()` loads the benchmarks of the [default catalog](../models/presets.md). The **starter catalog** shipped in the package has 16 benchmark signals, grouped by routing domain:

- Math and reasoning: **AIME-2024, AIME-2025, GPQA, HLE, AA-LCR**.
- Knowledge and professional domains: **MMLU-Pro, MMMU, LegalBench**.
- Code: **LiveCodeBench, SciCode**.
- Agentic tasks: **Tau2-bench, Terminal-bench-Hard**.
- Instruction following: **IFBench**.
- Human preference: **Chatbot Arena Elo**, plus its **Code** and **Vision** tracks.

After [`tryaii login`](../../cli/login.md) (free), the **full catalog** routes on a larger benchmark set. `BenchmarkRegistry.default()` takes the same `catalog` option as `Router` (`"auto"`, `"starter"`, `"full"`).

Each benchmark has a category mapping, a [normalization range](../routing/scoring.md#normalization), and an importance weight. All three are catalog data: they live in each catalog bundle's `benchmarks.json` and `normalization_ranges.json` (see [the catalog contract](../../catalog/CONTRACT-catalog-v1.md), Appendix A), so the same engine routes the starter catalog that ships in the package and the full catalog. `STANDARD_BENCHMARKS` is the starter catalog's set; `BenchmarkRegistry.from_bundle(bundle)` / `BenchmarkRegistry.fromBundle(bundle)` gives any other bundle's. The ranges are **fitted to the full catalog** (`lo` = the p25 of the benchmark's real scores across routable models, `hi` = their max) and copied unchanged into the starter catalog, so a model scores the same on a benchmark both catalogs share; the importance weights are hand-maintained, because they are editorial rather than catalog-derived. A benchmark with too few real scores keeps a hand-written range and is flagged `"fallback": true`. Each catalog's representative training queries (232 in the starter catalog) ship with it and are loaded by the centroid generator, so the `STANDARD_BENCHMARKS` definitions themselves have empty `training_queries`.

## BenchmarkDefinition

`name`, `description`, `training_queries` / `trainingQueries` (representative prompts), `normalization` (`NormalizationRange(min_score, max_score)`), `broad_category` / `broadCategory` (default `TECHNICAL`), `subcategories`, `metadata`. `to_dict`/`from_dict` (and Node module helpers `benchmarkToDict`/`benchmarkFromDict`) round-trip a snake_case JSON shape.

## Adding a custom benchmark

The easy path is the Router, which wires everything at once (definition + normalizer + centroid):

```python
router.add_benchmark(
    name="CustomerSupportQA",
    description="Customer support query handling quality",
    queries=["How do I reset my password?", "...10-20 representative prompts..."],
    min_score=0, max_score=100)

router.add_model("support-tuned-model", provider="custom",
                 benchmarks={"CustomerSupportQA": 88.0})   # give models scores on it
```

```ts
await router.addBenchmark('CustomerSupportQA', queries, 'description', 0, 100);
```

Effective immediately for subsequent routes. **Not persistent across processes** — the centroid cache file is validated against the standard benchmark-set fingerprint on startup, so re-add custom benchmarks when your app boots.

## Registry API

| Method | Notes |
|---|---|
| `register(definition)` / `unregister(name)` | Add/replace by name; remove |
| `get(name)` · `names` · `all_benchmarks`/`allBenchmarks` | Lookup/enumerate |
| `get_training_queries()` / `getTrainingQueries()` | Only benchmarks with non-empty queries (empty `{}` for the default registry — standard queries live in package data) |
| `get_normalizer()` / `getNormalizer()` | A `BenchmarkNormalizer` over all registered ranges |
| `load_from_file(path)` / `loadFromFile(path)` | JSON `{"benchmarks": [...]}`; returns the count — the interchange format for external benchmark-creation tools |
| `export_to_file(path)` / `exportToFile(path)` | Pretty-printed JSON (Python writes atomically) |
| `len()` / `length` · `in` / `has(name)` | |

A registry passed as `Router(benchmark_registry=...)` / `new Router({ benchmarkRegistry })` replaces the standard set entirely.
