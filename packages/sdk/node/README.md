# TryAii-DRE SDK (Node)

High-level Node.js/TypeScript SDK for TryAii-DRE (Differential Routing Engine).
Provides a unified `DREClient` that wraps model selection and the OpenRouter
API into a single interface with full async support and Express middleware.

Selection in the SDK is task-agnostic: models are ranked by overall quality
(mean of normalized benchmark scores), cost, and speed against the user's
priorities -- the prompt itself is not classified. For prompt-aware semantic
routing, use the [`tryaii-dre`](../../node) core package directly.

## Installation

```bash
npm install tryaii-dre-sdk
```

## Quick Start

```typescript
import { DREClient } from "tryaii-dre-sdk";

const client = new DREClient({ apiKey: "sk-or-..." });

// Route a prompt and get an AI response
const response = await client.chat("Write a Python quicksort implementation");
console.log(response.content);
console.log(response.modelUsed);

// Just route (no API call) to see which model would be selected
const result = client.route("Explain quantum computing");
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

app.use(dreMiddleware({ apiKey: "sk-or-..." }));

// Every response now includes X-DRE-Model and X-DRE-Score headers
```

## API Reference

### DREClient

| Method | Description |
|---|---|
| `chat(prompt, options?)` | Pick the best model for your priorities and return the response |
| `stream(prompt, options?)` | Pick the best model and stream the response as chunks |
| `route(prompt, options?)` | Pick the best model only -- returns RouteResult, no API call |

### Types

- `Priorities` -- `{ quality: number, cost: number, speed: number }` (1-5 scale)
- `RouteResult` -- `{ bestModel, scores, classification }`
- `ChatResponse` -- `{ content, modelUsed, openrouterModel, routeReasoning, usage }`
- `ChatOptions` -- `{ priorities?, systemMessage?, temperature?, maxTokens? }`

## License

Apache 2.0
