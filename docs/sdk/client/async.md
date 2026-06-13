# AsyncDREClient (Python only)

The asyncio twin of [`DREClient`](README.md): same constructor and methods, all `async`. Routing runs in a worker thread (`asyncio.to_thread`); HTTP uses `httpx.AsyncClient`. The Node SDK has no separate async client — its `DREClient` is already async.

```python
import asyncio
from tryaii import AsyncDREClient   # lazy-loaded; importing tryaii doesn't pull httpx

async def main():
    async with AsyncDREClient() as client:                 # context manager closes the HTTP client
        result = await client.route("Summarize this RFC")  # no API key needed
        resp = await client.chat("Summarize this RFC", max_tokens=300)
        print(resp.model_used, resp.content)

        async for chunk in client.stream("Explain recursion"):
            print(chunk, end="")

asyncio.run(main())
```

## Surface

`AsyncDREClient(api_key=None, priorities=None, embedding_model=None)` with `route`, `chat`, `stream`, `route_and_chat` — identical signatures and semantics to the sync client — plus `close()` / `async with` support.

## Differences from the sync client

- Requires `httpx` for `chat`/`stream` (`pip install tryaii[openrouter]`); the `ImportError` is raised lazily on the first API call, so pure `route()` works without it.
- Exposes `client.router` but **not** `client.openrouter` (it manages its own `httpx.AsyncClient`, timeout 120 s).
- Retry behavior matches the sync integration: statuses 429/500/502/503/504, up to 3 retries with exponential backoff + jitter, `Retry-After` honored on 429. Streaming retries only before the first byte is yielded — once content has flowed, a mid-stream failure re-raises rather than replaying.
