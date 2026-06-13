# OpenRouterIntegration

The lower-level "route then call" building block used by the [clients](README.md). Use it directly when you need a **custom Router** (custom registry, benchmarks, or embedding provider) behind real API calls, or the `override_model` escape hatch.

Imports — Python: `from tryaii.integrations import OpenRouterIntegration` (needs `pip install tryaii[openrouter]` for httpx). Node: `import { OpenRouterIntegration } from 'tryaii'` or from the `tryaii/integrations` subpath.

```python
from tryaii import Router
from tryaii.integrations import OpenRouterIntegration

router = Router()                                   # bring any custom router
with OpenRouterIntegration(router, app_name="my-app") as openrouter:   # key from OPENROUTER_API_KEY
    resp = openrouter.chat("Find the bug in this hook",
                           priorities={"quality": 5, "cost": 2, "speed": 3},  # plain dict
                           system_message="You are terse.", temperature=0, max_tokens=400)
    for chunk in openrouter.stream("Explain TCP vs UDP"):
        print(chunk, end="")
    resp = openrouter.chat("Hello!", override_model="gpt-4o-mini")     # skip routing entirely
```

```ts
import { Router, OpenRouterIntegration } from 'tryaii';

const integration = new OpenRouterIntegration(new Router(),
  { apiKey: process.env.OPENROUTER_API_KEY!, appName: 'my-app' });   // NO env fallback on Node
const resp = await integration.chat('Find the bug', { priorities: { quality: 5 }, maxTokens: 400 });
for await (const chunk of integration.stream('Explain TCP vs UDP')) process.stdout.write(chunk);
```

## API

- `chat(prompt, …) → OpenRouterResponse` and `stream(prompt, …)` yielding content chunks. Options: `priorities` (plain dict, partial OK), `system_message`/`systemMessage`, `temperature` (default 0.7), `max_tokens`/`maxTokens` (sent only when finite and > 0), `override_model`/`overrideModel`.
- Python: `close()` + sync context manager; retries 429/5xx up to 3 times with backoff, honors `Retry-After`; stream retries only before the first yielded byte.
- Base URL is fixed to `https://openrouter.ai/api/v1`. The `X-Title` header is the `app_name`/`appName` (default `tryaii`).

## OpenRouterResponse

`content`, `model_used`/`modelUsed` (tryaii model ID), `openrouter_model`/`openrouterModel` (OpenRouter slug), `route_reasoning`/`routeReasoning`, `usage` (raw dict), `raw_response`/`rawResponse`.

## Model ID mapping

`MODEL_ID_TO_OPENROUTER` (exported in both SDKs) maps each of the 33 default-preset model IDs to its OpenRouter slug (e.g. `gpt-4o → openai/gpt-4o`, `claude-sonnet-4-5-20250929 → anthropic/claude-sonnet-4.5`). Unknown IDs pass through unchanged — so custom models work if their ID is already a valid OpenRouter slug.

## API-key behavior differs by SDK

- **Python** falls back to `OPENROUTER_API_KEY`; a missing key raises lazily on the first request.
- **Node** requires an **explicit** `apiKey` (no env fallback — that convenience lives in [`DREClient`](README.md)); a missing key throws on the first call.
