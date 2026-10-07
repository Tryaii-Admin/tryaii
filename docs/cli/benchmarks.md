# `tryaii benchmarks` — list registered benchmarks

Print the benchmarks of the catalog in use (see [SDK benchmarks](../sdk/benchmarks/README.md)). The starter catalog shipped in the package has 16; after [`tryaii login`](login.md) the full catalog adds more.

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
Available Benchmarks (16):
------------------------------------------------------------
  AIME-2024                      [23.3-99.583]   AIME 2024 competition mathematics
  AIME-2025                      [35.5833-99.0]  AIME 2025 competition mathematics
  GPQA                           [59.3-96.3]     Graduate-level science question answering
  ...
```

Each line shows the benchmark name, its raw-score normalization range, and description. The starter catalog's set is:

- Math and reasoning: AIME-2024, AIME-2025, GPQA, HLE, AA-LCR.
- Knowledge and professional domains: MMLU-Pro, MMMU, LegalBench.
- Code: LiveCodeBench, SciCode.
- Agentic tasks: Tau2-bench, Terminal-bench-Hard.
- Instruction following: IFBench.
- Human preference: Chatbot Arena Elo plus its Code and Vision tracks.
