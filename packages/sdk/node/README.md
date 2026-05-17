# TryAii-DRE SDK (Node)

High-level Node.js/TypeScript SDK for TryAii-DRE (Differential Routing Engine).
Provides a unified `DREClient` that wraps model selection and the OpenRouter
API into a single interface with full async support and Express middleware.

Selection is **prompt-aware**: under the hood the SDK delegates routing to the
[`tryaii-dre`](../../node) core `Router`, which embeds the prompt and matches
it against benchmark centroids before scoring models against your quality /
cost / speed priorities.

## Installation

```bash
npm install tryaii-dre-sdk
```

### What gets installed

`tryaii-dre-sdk` depends on `tryaii-dre` (the core routing engine), which in
turn has an **optional** dependency on [`@xenova/transformers`](https://www.npmjs.com/package/@xenova/transformers)
for local prompt embeddings. Be aware of what that pulls in:

- **~100 MB of native binaries**. `@xenova/transformers` brings in
  `onnxruntime-node`, which ships prebuilt CPU/GPU binaries per platform.
  `npm install` will download these by default; the install step is what
  grows your `node_modules`, not the JS code.
- **No network access is needed to install the model itself** — the embedding
  model is downloaded lazily on first use (see below).
- It is declared as `optionalDependencies` on the core package, so an install
  failure on an unsupported platform will not break `npm install`, but
  `DREClient.route()` / `chat()` / `stream()` will throw on first call.

If you cannot accept the native binary (airgapped builds, constrained Lambda
layers, etc.), use the core `tryaii-dre` package directly and inject a custom
`BaseEmbeddingProvider` — the SDK wrapper does not yet expose an injection
hook.

### First-call cost

**The first call to `route()`, `chat()`, or `stream()` blocks for several
seconds to a minute and performs network I/O.** Specifically:

1. **Model download (one-time, ~25 MB).** The default embedding model
   (`Xenova/all-MiniLM-L6-v2`) is fetched from the HuggingFace CDN
   (`huggingface.co`) and cached. Make sure outbound HTTPS to
   `huggingface.co` is allow-listed in your environment.
2. **Centroid cache write.** Benchmark centroids are written to
   `~/.tryaii_dre/centroids/centroids_all-MiniLM-L6-v2.json`. On read-only
   filesystems (Lambda `/var/task`, some containers) the first call will
   fail. Point `HOME` at a writable directory, or use the core package
   directly to override `config.dataDir`.
3. **ONNX session warm-up.** First inference compiles the model graph.

Subsequent calls in the same process hit in-memory caches and return in
milliseconds. You can pre-warm by calling `client.route('warmup')` at app
startup before serving requests.

## Quick Start

```typescript
import { DREClient } from "tryaii-dre-sdk";

const client = new DREClient({ apiKey: "sk-or-..." });

// Route a prompt and get an AI response
const response = await client.chat("Write a Python quicksort implementation");
console.log(response.content);
console.log(response.modelUsed);

// Just route (no API call) to see which model would be selected
const result = await client.route("Explain quantum computing");
console.log(result.bestModel);
console.log(result.scores);

// Stream a response
for await (const chunk of client.stream("Explain machine learning")) {
  process.stdout.write(chunk);
}
```

## Custom Priorities

Control the quality/cost/speed tradeoff:

```typescript
const client = new DREClient({
  apiKey: "sk-or-...",
  priorities: { quality: 5, cost: 1, speed: 2 },
});

// Or per-request:
const response = await client.chat("Optimize this SQL query", {
  priorities: { quality: 5, cost: 1, speed: 2 },
});
```

## Express Middleware

Add DRE routing headers to your Express application:

```typescript
import express from "express";
import { dreMiddleware } from "tryaii-dre-sdk/middleware";

const app = express();

app.use(
  dreMiddleware({
    apiKey: "sk-or-...",
    // Routing failures never block the request pipeline. Without an onError
    // hook they are silently swallowed — pass one to observe them.
    onError: (err) => console.error("[dre] routing failed:", err),
  }),
);

// Every response now includes X-DRE-Model and X-DRE-Score headers
```

## API Reference

### DREClient

| Method | Description |
|---|---|
| `chat(prompt, options?)` | Pick the best model for your priorities and return the response |
| `stream(prompt, options?)` | Pick the best model and stream the response as chunks |
| `route(prompt, options?)` | Pick the best model only -- async, returns RouteResult, no API call |

### Types

- `Priorities` -- `{ quality: number, cost: number, speed: number }` (1-5 scale)
- `RouteResult` -- `{ bestModel, scores, bestScore, bestReasoning, priorities }`
- `ChatResponse` -- `{ content, modelUsed, openrouterModel, routeReasoning, usage }`
- `ChatOptions` -- `{ priorities?, systemMessage?, temperature?, maxTokens? }`
- `DREMiddlewareOptions` -- `{ apiKey?, priorities?, headerPrefix?, promptField?, onError? }`

## Eval Dashboard

`renderDashboard`, `DashboardSummary`, and `DashboardLinks` are re-exported from `tryaii-dre` for convenience -- import them directly from `tryaii-dre-sdk` if you'd rather not pull in the core package by name. See the [core README](../../node/README.md#eval-dashboard) for usage.

## Error handling

`chat()` and `stream()` throw loudly rather than silently falling back:

- **Missing API key** — Throws `"DREClient requires an OpenRouter API key..."`
  before any routing or network work.
- **Routing returned no model** — If your registry is empty or filters remove
  every candidate, `chat()`/`stream()` throw
  `"DREClient.chat: routing returned no model..."` rather than substituting
  a hard-coded default.
- **OpenRouter HTTP error** — Non-2xx responses throw with the status code
  and response body.

`route()` does not call OpenRouter, so it does not require an API key and
will return an empty `RouteResult` (`bestModel: ""`, `scores: []`) if no
models match.

## License

Apache 2.0
