# tryaii

AI model router for Node.js and TypeScript.

Ranks models using benchmark performance, pricing, latency, and your quality/cost/speed priorities.

## Installation

```bash
npm install tryaii
```

## Quick Start

```typescript
import { DREClient, Priorities, Router } from 'tryaii';

// Embedding-based classifier using @xenova/transformers.
const router = new Router();

const result = await router.route('Write a Python function to sort an array');
console.log(result.bestModel);     // e.g., "gpt-5.2"
console.log(result.scores[0]);     // Full scoring breakdown

// Route with custom priorities
const budgetResult = await router.route(
  'Explain quantum computing',
  { priorities: Priorities.budget() }  // Favor cheaper models
);

// Route and call the selected model through OpenRouter.
const client = new DREClient({ apiKey: process.env.OPENROUTER_API_KEY });
const response = await client.chat('Write a quicksort implementation');
console.log(response.content);
```

## CLI

Installing the package adds a `tryaii` command (same surface as the Python SDK). It
opens with an animated blue→red banner, then runs your command. The banner prints to
stderr and auto-suppresses when output is piped, so `--json` stays clean.

```bash
tryaii route "Write a Python function to merge sorted arrays" --quality=5 --cost=1
tryaii eval prompts.json --output results/run --quality=5 --cost=1 --speed=1
tryaii eval prompts.json --max-price=0.10 --output-tokens=2000 --budget-mode=fit-output
tryaii models --provider anthropic        # add --json for machine-readable output
tryaii benchmarks --json
tryaii setup                               # download the embedding model + warm centroids
```

| Command | Key options |
|---------|-------------|
| `route "<prompt>"` | `--quality/--cost/--speed <1-5>` (default 3), `--top-k <n>` |
| `eval <input.json>` | `-o/--output <dir>`, `--max-price <usd>`, `--output-tokens <n>`, `--budget-mode strict\|fit-output` |
| `models` | `--provider <name>`, `--json` |
| `benchmarks` | `--json` |
| `setup` / `regenerate` | `--model <name>` |

Global flags: `--no-banner` (or `TRYAII_NO_BANNER=1`), `NO_COLOR=1`, `-v/--verbose`,
`-V/--version`. All flags work in any position and match the PyPI CLI. See the
[repo README](../../README.md#command-line-interface) for the full reference.

## Embedding Provider

`Router` uses semantic embeddings to classify prompts against benchmark centroids. The default provider is `LocalEmbeddingProvider` (backed by `@xenova/transformers`), which runs an ONNX MiniLM model locally with no API keys. You can supply a custom provider via the `embeddingProvider` option.

### Sync vs. async

`router.route()` is **async** -- it works with any embedding provider, including the default `LocalEmbeddingProvider` (which is async-only because the underlying ONNX runtime is async).

For the niche case where you have a sync embedding provider (e.g. a custom in-process provider that doesn't do I/O), `router.routeSync()` gives you a blocking call. Calling `routeSync()` with an async-only provider throws a clear error pointing you back to `route()`.

```typescript
// Default async path -- works with any provider
const result = await router.route('Write a sorting algorithm');

// Sync path -- requires a sync provider (e.g. injected via the constructor)
const sync = router.routeSync('Write a sorting algorithm');
```

## Priorities

Control what matters most in model selection:

```typescript
// Presets
Priorities.balanced()     // quality=3, cost=3, speed=3
Priorities.performance()  // quality=5, cost=1, speed=1
Priorities.budget()       // quality=2, cost=5, speed=3
Priorities.fast()         // quality=2, cost=3, speed=5

// Custom
new Priorities(4, 2, 3)   // quality=4, cost=2, speed=3
```

## Adding Custom Models

```typescript
router.addModel({
  modelId: 'my-custom-model',
  provider: 'custom',
  benchmarks: { 'HumanEval': 85, 'MMLU': 80 },
  pricing: [0.001, 0.002],  // [input, output] per 1k tokens
  latency: 'fast',
});
```

## Adding Custom Benchmarks

`addBenchmark()` is async -- it generates a centroid for the new benchmark using the configured embedding provider, which means it works with the default async `LocalEmbeddingProvider`. Subsequent `route()` calls immediately see the new benchmark.

```typescript
await router.addBenchmark(
  'CustomerSupportQA',
  [
    'How do I reset my password?',
    'I want to cancel my subscription',
    'Where is my order?',
  ],
  'Customer support query handling',
  0,    // min score
  100,  // max score
);
```

For sync-provider setups there's a `router.addBenchmarkSync(...)` sibling that blocks on centroid generation.

## Filtering

```typescript
// Only Anthropic models
await router.route('prompt', { filterProvider: 'anthropic' });

// Only models under $0.01/1k input tokens
await router.route('prompt', { filterMaxCost: 0.01 });

// Only models with specific capabilities
await router.route('prompt', { filterCapability: 'vision' });
```

## High-Level Client

Use `DREClient` when you want routing plus chat/streaming in one object:

```typescript
import { DREClient } from 'tryaii';

const client = new DREClient({ apiKey: process.env.OPENROUTER_API_KEY });

const route = await client.route('Explain quantum computing');
console.log(route.bestModel);

for await (const chunk of client.stream('Explain machine learning')) {
  process.stdout.write(chunk);
}
```

## OpenRouter Integration

Route prompts and call the selected model through OpenRouter:

```typescript
import { OpenRouterIntegration, Router } from 'tryaii';

const router = new Router();
const openrouter = new OpenRouterIntegration(router, {
  apiKey: process.env.OPENROUTER_API_KEY,
});

const response = await openrouter.chat('Write a quicksort in Python');
console.log(response.modelUsed);   // Which model was selected
console.log(response.content);     // The actual response
```

## Architecture

```
User Prompt
     |
     v
[Classifier] --> benchmark similarity scores
     |              (HumanEval: 0.8, MMLU: 0.3, ...)
     v
[ScoringEngine] --> weighted scores per model
     |              (quality * qW + cost * cW + speed * sW)
     v
[RouteResult] --> best model + reasoning
```

## Eval Dashboard

The `tryaii eval` command (above) supports a shared generation budget:

```bash
tryaii eval prompts.json --output results/node-budget --max-price=0.10 --output-tokens=2000
tryaii eval prompts.json --output results/node-budget-fit --max-price=0.10 --output-tokens=2000 --budget-mode=fit-output
```

This treats `--max-price` as the total budget for the whole dataset and
`--output-tokens` as the fixed expected response length per prompt. In budgeted
eval, quality/cost/speed priority flags are ignored: price is the hard
constraint, and the optimizer maximizes model quality within that price.
`--budget-mode=fit-output` lowers that fixed output length when the requested
length cannot fit the total budget. Each run writes `results.jsonl`,
`summary.json`, and an `index.html` dashboard.

You can also render that dashboard programmatically from any eval run's `summary.json`
(the same shape the CLI writes). The output is a zero-dependency string you can write next
to `summary.json` / `results.jsonl` to make the run dir an openable artifact.

```ts
import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { renderDashboard, type DashboardSummary } from 'tryaii';

const runDir = './runs/quality';
const summary: DashboardSummary = JSON.parse(
  await readFile(join(runDir, 'summary.json'), 'utf8'),
);

const html = renderDashboard(summary, runDir);
await writeFile(join(runDir, 'index.html'), html, 'utf8');
```

Pass `{ summaryHref, resultsHref }` as the third argument to override the footer artifact links (useful when rendering to a directory that doesn't sit next to the JSON).

## Requirements

- Node.js >= 18.0.0
- TypeScript >= 5.3 (for development)

## License

Apache 2.0
