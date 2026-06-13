# `index.html` — the eval dashboard

A single self-contained HTML file rendering the run's [summary](summary-json.md). No external assets, no JavaScript — open it from disk, attach it to a PR, or serve it statically. Node and Python generate identical markup, so reports look the same regardless of which SDK ran the eval.

## Layout

1. **Header** — `tryaii routing eval · N prompts`, generation timestamp (UTC ISO-8601), and the input file path.
2. **Priority chips** — the run's quality/cost/speed values (color-coded by level). In budget runs these show the flags as given, even though selection ignored them — check `summary.json`'s `budget.prioritiesIgnored`.
3. **Stat cards** — Successes, Errors (highlighted when > 0), Distinct models, Avg route ms.
4. **Recommended models — overall** — horizontal bar list of the `distribution` (model, share bar, count, pct).
5. **By category** — one card per dataset `category`: prompt count, top-3 models with share bars, top-5 benchmarks by average similarity.
6. **Footer** — relative links to `summary.json` and `results.jsonl` (keep the three artifacts together).

## Styling

Dark theme by default, automatic light theme via `prefers-color-scheme`. Responsive (cards reflow, stats grid collapses on narrow screens). All model/category/benchmark names are HTML-escaped.

## Notes

- The dashboard intentionally shows the *summary level only* — per-prompt drill-down (including budget fields like `budgetConstrained`) lives in [`results.jsonl`](results-jsonl.md).
- Rebuild or customize a dashboard from any `summary.json` programmatically with the Node SDK's [`renderDashboard`](../../../sdk/dashboard.md) export (the Python renderer is internal to the CLI).
