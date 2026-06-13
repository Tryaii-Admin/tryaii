# Eval dashboard

[`tryaii eval`](../cli/eval/README.md) writes a self-contained `index.html` dashboard (no external assets or JS; dark/light via `prefers-color-scheme`) showing stat cards, the overall recommended-model distribution, and per-category breakdowns. Both SDKs generate identical markup.

## Rendering programmatically (Node export)

The Node SDK exports the renderer, so you can build dashboards from your own routing runs:

```ts
import { renderDashboard, type DashboardSummary } from 'tryaii';

const summary: DashboardSummary = {
  totalPrompts: 120, successCount: 118, errorCount: 2,
  distinctModels: 7, avgRouteMs: 41.2, totalRouteMs: 4944,
  priorities: { quality: 5, cost: 1, speed: 1 },
  distribution: [{ model: 'claude-sonnet-4-5-20250929', count: 60, pct: 50.85 }, /* ... */],
  byCategory: [{ category: 'code', count: 80,
                 topModels: [{ model: '...', count: 40, pct: 50 }],
                 topBenchmarks: [{ name: 'HumanEval', avgScore: 0.91 }] }],
};

const html = renderDashboard(summary, 'prompts.json',
  { summaryHref: 'summary.json', resultsHref: 'results.jsonl' });  // optional footer links (these are the defaults)
```

`renderDashboard(summary, inputPath, links?) → string` is a pure function — it returns the full HTML document; write it wherever you like. The `DashboardSummary` shape is exactly what `eval` writes to `summary.json`, so you can also re-render an existing run's summary.

## Python

The Python renderer is internal to the CLI (`tryaii.cli.main`) and not part of the public API — generate dashboards by running `tryaii eval`, or use the Node export for programmatic rendering.
