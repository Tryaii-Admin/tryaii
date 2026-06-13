# DREClient — route *and* call

`DREClient` is the highest-level entry point: route the prompt, then call the winning model through OpenRouter, in one method. Needs an OpenRouter key for `chat`/`stream` (pass `api_key`/`apiKey` or set `OPENROUTER_API_KEY`); `route()` alone needs no key.

```python
from tryaii import DREClient

client = DREClient()                                  # key from OPENROUTER_API_KEY
resp = client.chat("Explain CAP theorem", priorities=Priorities(quality=5, cost=2, speed=3))
print(resp.model_used, resp.content)

for chunk in client.stream("Write a haiku about routing"):
    print(chunk, end="")
```

```ts
import { DREClient } from 'tryaii';

const client = new DREClient({ apiKey: process.env.OPENROUTER_API_KEY,
                               priorities: { quality: 5 } });   // partial dict OK, rest default to 3
const resp = await client.chat('Explain CAP theorem', { temperature: 0.7, maxTokens: 500 });
console.log(resp.modelUsed, resp.content);

for await (const chunk of client.stream('Write a haiku about routing')) process.stdout.write(chunk);
```

## Constructor

| Python | Node |
|---|---|
| `DREClient(api_key=None, priorities=None, embedding_model=None)` | `new DREClient({ apiKey?, priorities?, baseUrl? })` |

- Missing key → error on the first `chat`/`stream` (not at construction).
- Node accepts a configurable `baseUrl` (default `https://openrouter.ai/api/v1`); Python configures the embedding model instead.
- Neither accepts a custom `Router` — for custom registries/providers, drop down to [`Router`](../routing/README.md) + [`OpenRouterIntegration`](openrouter.md).

## Methods

| Method | Python | Node | Notes |
|---|---|---|---|
| `route(prompt, priorities?, top_k=5)` | ✓ | ✓ | Routing only, no API call. Node returns the simplified `ClientRouteResult` shape (`bestModel, scores, bestScore, bestReasoning, priorities`); Python returns the core `RouteResult`. |
| `chat(prompt, priorities?, system_message?, temperature=0.7, max_tokens?)` | ✓ | ✓ (options object: `systemMessage`, `maxTokens`) | Routes, then POSTs `/chat/completions`. `max_tokens` is only sent when finite and > 0. |
| `stream(...)` (same params) | ✓ generator | ✓ async generator | Yields content chunks; ends at `[DONE]`. |
| `route_and_chat(...)` | ✓ | ✗ | Routes once, then chats with `override_model` so the API call reuses the decision; returns `(RouteResult, OpenRouterResponse)`. |

Underlying objects are exposed for advanced use: Python `client.router` / `client.openrouter`; Node keeps them private.

## Response shape

Python returns [`OpenRouterResponse`](openrouter.md) (`content, model_used, openrouter_model, route_reasoning, usage` dict, `raw_response`). Node returns `ChatResponse` — same fields camelCased, with `usage` typed as `TokenUsage { promptTokens?, completionTokens?, totalTokens? }`.

## Errors

- Routing returns no model (over-filtered/empty registry) → `routing returned no model for this prompt`.
- Missing API key → descriptive error before routing.
- HTTP errors → Node: `OpenRouter API error (<status>): <body>`; Python: `httpx.HTTPStatusError` after retries (429/5xx retried up to 3 times with backoff; `Retry-After` honored).
- 200-with-error envelope / in-stream error chunk → `OpenRouter API error: ...` / `OpenRouter stream error: ...`.

For asyncio, use Python's [`AsyncDREClient`](async.md). For routing without calling, prefer plain [`Router`](../routing/README.md).
