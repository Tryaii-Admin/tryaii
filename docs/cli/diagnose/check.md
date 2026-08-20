# `tryaii diagnose check` — run the checks over an inventory

```bash
tryaii diagnose check inventory.json --quality 3 --cost 4 --speed 2 --calls-per-day 1000 --goal "reduce prices"
cat inventory.json | tryaii diagnose check - --json
```

Reads an agent-written inventory of LLM call sites (shape: [`plan
--json`](plan.md)), runs the selected checks, writes the run to
`<out-dir>/<run-id>/` (`inventory.json` verbatim, `findings.json`,
`meta.json`) plus the `latest` pointer, and prints a summary (or the full
findings JSON with `--json`).

## Flags

| Flag | Default | Effect |
|---|---|---|
| `<inventory.json \| ->` | required | Inventory file, or `-` for stdin |
| `--quality/--cost/--speed <1-5>` | 3 | Priorities for model_fit + the interview record |
| `--checks <list>` | all | Comma-separated subset of `model_fit,cache_readiness,cost_exposure,hygiene`; unselected checks report `skipped` |
| `--calls-per-day <n>` | — | Default traffic assumption (per-site values win); without any traffic, monthly figures are itemized `insufficient data` |
| `--output-tokens <n>` | 500 | Default output tokens per call |
| `--goal <text>` | — | The user's stated goal (echoed into findings + report) |
| `--out-dir <dir>` | `.tryaii/diagnose` | Run store directory |
| `--run-id <id>` / `--now <iso8601>` | UTC now | Determinism seam (fixtures/CI); run ids sort lexicographically |
| `--json` | off | Print the findings document instead of the summary |
| `--no-daemon` | off | Classify in-process; do not use or start a daemon |

## Degradation rules (per site, never per run)

| Missing | Effect |
|---|---|
| `file`/`line` | site skipped at intake, listed with a reason |
| `prompt` | all four checks `insufficient data` |
| `provider` | cache_readiness insufficient (hygiene still runs — it is provider-free) |
| `model` unknown/absent | model_fit shows the recommendation but no comparison; cost insufficient |
| traffic | per-call cost only; `monthly: insufficient data` |

Model ids resolve exactly, via OpenRouter slugs, or via separator/case
normalization — never fuzzily. Sites may carry a precomputed
`_classification` (the injection seam); everything else classification-wise
requires [`tryaii setup`](../setup.md) once.

Besides the overall best model, model_fit also recommends the **best model
in the current model's price range** (`recommended_same_price`): the
highest-ranked model whose blended per-1k price sits within ±20% of the
current model's — a quality upgrade that costs what you already pay. When
the current model wins its own band, that is reported as a positive
(`is_current: true`); unresolved/unpriced current models get `null`.

Cost figures are estimates; **cache savings are an upper bound** built on
conservative per-provider read-discount factors
(`shared/diagnose/costmodel.json`). The cheaper-swap rule: the cheapest
ranked model whose raw quality score is within 0.05 of the current model's.

Exit codes: 0 checks completed (findings included — warn-only) · 1 runtime
failure (missing file, setup gate) · 2 usage error or invalid JSON.
