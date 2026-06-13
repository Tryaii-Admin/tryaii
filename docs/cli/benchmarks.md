# `tryaii benchmarks` — list registered benchmarks

Print the 12 standard benchmarks the router scores against (see [SDK benchmarks](../sdk/benchmarks/README.md)).

```bash
tryaii benchmarks
tryaii benchmarks --json
```

## Flags

| Flag | Type | Default | Effect |
|---|---|---|---|
| `--json` | boolean | off | Pretty-printed JSON: `name`, `description`, `training_queries`, `normalization {min_score, max_score}`, `broad_category`, `subcategories`, `metadata` |

## Text output

```
Available Benchmarks (12):
------------------------------------------------------------
  MMLU                           [25-95]         General knowledge across 57 subjects
  HumanEval                      [20-95]         Python code generation correctness
  ...
```

Each line shows the benchmark name, its raw-score normalization range, and description. The full set: MMLU, HellaSwag, HumanEval, SWE-bench, TruthfulQA, ARC, GSM8K, DROP, SuperGLUE, Chatbot Arena (LMSys), MT-Bench, LiveBench.
