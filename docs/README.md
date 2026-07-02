# TryAii Documentation

TryAii ships as two packages with identical behavior — `tryaii` on PyPI (Python ≥ 3.9) and `tryaii` on npm (Node ≥ 18) — each installing the same `tryaii` CLI.

| Section | What's inside |
|---|---|
| [`cli/`](cli/README.md) | Every `tryaii` CLI command, flag-by-flag: [route](cli/route.md), [eval](cli/eval/README.md) (+ [dataset](cli/eval/dataset/README.md) · [priority mode](cli/eval/priority-mode/README.md) · [budget mode](cli/eval/budget-mode/README.md) · [outputs](cli/eval/outputs/README.md)), [models](cli/models.md), [benchmarks](cli/benchmarks.md), [setup](cli/setup.md), [regenerate](cli/regenerate.md) |
| [`sdk/`](sdk/README.md) | Every way to use the SDKs, by capability: [routing](sdk/routing/README.md), [client](sdk/client/README.md), [budget routing](sdk/budget/README.md), [models](sdk/models/README.md), [benchmarks](sdk/benchmarks/README.md), [embeddings](sdk/embeddings/README.md), [configuration](sdk/configuration/README.md), [dashboard](sdk/dashboard.md) |
| [`api-reference/`](api-reference/README.md) | The separate hosted TryAii-Bench REST API (benchmark data service) |

## Install

```bash
pip install tryaii        # Python 3.9+
npm install tryaii        # Node 18+
```

Routing runs fully locally (on-device embeddings) — no API key is needed to rank models. An `OPENROUTER_API_KEY` is only required when the SDK should *call* the chosen model.
