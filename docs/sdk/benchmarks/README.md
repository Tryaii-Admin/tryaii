# Benchmarks

A *benchmark* is the unit of routing signal: a name, a normalization range, and a set of training queries whose [centroid](centroids.md) the prompt is compared against. Models earn quality from their scores on the benchmarks most similar to the prompt.

Imports — Node: `BenchmarkRegistry`, `BenchmarkDefinition`, `STANDARD_BENCHMARKS` from the package root. Python: `BenchmarkRegistry` from `tryaii`; `BenchmarkDefinition`, `STANDARD_BENCHMARKS` from `tryaii.benchmarks`.

## The 12 standard benchmarks

`BenchmarkRegistry.default()` ships: **MMLU, HellaSwag, HumanEval, SWE-bench, TruthfulQA, ARC, GSM8K, DROP, SuperGLUE, Chatbot Arena (LMSys), MT-Bench, LiveBench** — each with a category mapping and a [normalization range](../routing/scoring.md#normalization). Their training queries (15 each) ship as package data and are loaded by the centroid generator, so the `STANDARD_BENCHMARKS` definitions themselves have empty `training_queries`.

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
