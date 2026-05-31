"""
AsyncDREClient -- async version of DREClient for TryAii-DRE.

Provides the same interface as DREClient but with async/await support.
Uses asyncio.to_thread() for CPU-bound Router calls and httpx.AsyncClient
for non-blocking API calls.

Usage:
    from tryaii_dre.async_client import AsyncDREClient

    client = AsyncDREClient(api_key="sk-or-...")
    response = await client.chat("Write a quicksort in Python")
    print(response.model_used, response.content)
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from tryaii_dre.classifiers.base import MAX_PROMPT_LENGTH
from tryaii_dre.config import TryaiiDreConfig
from tryaii_dre.integrations.openrouter import (
    MODEL_ID_TO_OPENROUTER,
    OpenRouterResponse,
)
from tryaii_dre.router import Router, RouteResult
from tryaii_dre.scoring.priorities import Priorities

logger = logging.getLogger("tryaii_dre.async_client")

try:
    import httpx
except ImportError:  # pragma: no cover - exercised only without optional extra
    httpx = None


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class AsyncDREClient:
    """
    Async high-level client that combines routing and API calls.

    Same interface as DREClient but all methods are async. Router calls
    are offloaded to a thread pool via asyncio.to_thread(), and API calls
    use httpx.AsyncClient for true async I/O.

    Args:
        api_key: OpenRouter API key. Falls back to OPENROUTER_API_KEY env var.
        priorities: Default priorities for all routing calls.
        embedding_model: Sentence-transformers model name for embeddings.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        priorities: Optional[Priorities] = None,
        embedding_model: Optional[str] = None,
    ):
        self._api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self._default_priorities = priorities
        self._embedding_model = embedding_model

        # Build config
        config = TryaiiDreConfig()
        if embedding_model:
            config = TryaiiDreConfig(embedding_model=embedding_model)

        # Core router (sync -- will be called via asyncio.to_thread)
        self._router = Router(config=config)

        # Async HTTP client (lazy-initialized)
        self._http_client: Optional[httpx.AsyncClient] = None

    def _ensure_http_client(self) -> httpx.AsyncClient:
        """Lazy-initialize the async HTTP client."""
        if httpx is None:
            raise ImportError(
                "httpx is required for AsyncDREClient. "
                "Install with: pip install tryaii-dre[openrouter]"
            )
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                base_url=OPENROUTER_BASE_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "X-Title": "tryaii-dre",
                    "Content-Type": "application/json",
                },
                timeout=120.0,
            )
        return self._http_client

    @staticmethod
    def _resolve_model(model_id: str) -> str:
        """Convert TryAii-DRE model ID to OpenRouter slug."""
        return MODEL_ID_TO_OPENROUTER.get(model_id, model_id)

    # -- Validation / payload helpers ----------------------------------

    @staticmethod
    def _validate_prompt(prompt: str) -> str:
        """Reject empty/non-string prompts and truncate to MAX_PROMPT_LENGTH.

        Mirrors the sync OpenRouterIntegration so both SDKs behave identically.
        """
        if not prompt or not isinstance(prompt, str):
            raise ValueError("prompt must be a non-empty string")
        if len(prompt) > MAX_PROMPT_LENGTH:
            prompt = prompt[:MAX_PROMPT_LENGTH]
        return prompt

    @staticmethod
    def _build_payload(
        openrouter_model: str,
        messages: list,
        temperature: float,
        max_tokens: Optional[int],
        stream: bool = False,
    ) -> dict:
        """Build an OpenRouter chat payload, mirroring the sync integration."""
        payload: dict = {
            "model": openrouter_model,
            "messages": messages,
            "temperature": temperature,
        }
        if stream:
            payload["stream"] = True
        # Only forward max_tokens when it is a positive, finite value.
        if max_tokens is not None and math.isfinite(max_tokens) and max_tokens > 0:
            payload["max_tokens"] = max_tokens
        return payload

    # -- Retry helper --------------------------------------------------

    _RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
    _MAX_RETRIES = 3

    async def _post_with_retry(self, client, url: str, **kwargs):
        """POST with exponential-backoff retry on transient errors (async).

        Retries on 429 (rate-limit) and 5xx server errors up to ``_MAX_RETRIES``
        times, honoring the ``Retry-After`` header on 429. Mirrors the sync
        ``OpenRouterIntegration._request_with_retry``.
        """
        last_exc: Exception | None = None
        for attempt in range(self._MAX_RETRIES + 1):
            try:
                response = await client.post(url, **kwargs)
                if response.status_code not in self._RETRYABLE_STATUS_CODES:
                    return response
                last_exc = httpx.HTTPStatusError(
                    f"HTTP {response.status_code}",
                    request=response.request,
                    response=response,
                )
                if attempt == self._MAX_RETRIES:
                    response.raise_for_status()
                    return response  # pragma: no cover
                wait = self._backoff_wait(attempt, response)
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt == self._MAX_RETRIES:
                    raise
                wait = 2 ** attempt + random.uniform(0, 1)

            logger.warning(
                "Async request to %s failed (attempt %d/%d), retrying in %.1fs",
                url,
                attempt + 1,
                self._MAX_RETRIES + 1,
                wait,
            )
            await asyncio.sleep(wait)

        raise last_exc  # type: ignore[misc]

    @staticmethod
    def _backoff_wait(attempt: int, response) -> float:
        """Calculate wait time, honoring Retry-After on 429."""
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            if retry_after is not None:
                # Retry-After may be either delta-seconds or an HTTP-date.
                try:
                    return float(retry_after)
                except (ValueError, TypeError):
                    pass
                try:
                    then = parsedate_to_datetime(retry_after)
                    if then is not None:
                        if then.tzinfo is None:
                            then = then.replace(tzinfo=timezone.utc)
                        delta = (then - datetime.now(timezone.utc)).total_seconds()
                        return max(0.0, delta)
                except (ValueError, TypeError):
                    pass
        return 2 ** attempt + random.uniform(0, 1)

    @staticmethod
    def _content_from_data(data: dict) -> str:
        """Extract message content, raising on a 200-with-error envelope.

        OpenRouter may return HTTP 200 with an {"error": ...} body and no
        choices; 'error' may be a dict or a bare string.
        """
        choices = data.get("choices")
        if not choices:
            error = data.get("error")
            if isinstance(error, dict):
                msg = error.get("message", "No choices returned")
            elif isinstance(error, str):
                msg = error
            else:
                msg = "No choices returned"
            raise ValueError(f"OpenRouter API error: {msg}")
        return choices[0].get("message", {}).get("content", "")

    @property
    def router(self) -> Router:
        """Access the underlying Router instance."""
        return self._router

    async def route(
        self,
        prompt: str,
        priorities: Optional[Priorities] = None,
        top_k: int = 5,
    ) -> RouteResult:
        """
        Route a prompt without making an API call (async).

        The Router's classify/score logic is CPU-bound, so it is
        offloaded to a thread to avoid blocking the event loop.

        Args:
            prompt: The user message to classify and route.
            priorities: Override default priorities for this call.
            top_k: Number of top models to include in results.

        Returns:
            RouteResult with best_model, scores, and classification.
        """
        prio = priorities or self._default_priorities
        return await asyncio.to_thread(
            self._router.route, prompt, priorities=prio, top_k=top_k
        )

    async def chat(
        self,
        prompt: str,
        priorities: Optional[Priorities] = None,
        system_message: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> OpenRouterResponse:
        """
        Route the prompt to the best model and return the AI response (async).

        Args:
            prompt: The user message to route and send.
            priorities: Override default priorities for this call.
            system_message: Optional system prompt.
            temperature: Sampling temperature (0.0 to 2.0).
            max_tokens: Maximum tokens in the response.

        Returns:
            OpenRouterResponse with content, model_used, and routing info.
        """
        prompt = self._validate_prompt(prompt)

        # Route in a thread (CPU-bound)
        route_result = await self.route(prompt, priorities=priorities)
        model_id = route_result.best_model
        reasoning = route_result.scores[0].reasoning if route_result.scores else ""

        # Never POST an empty model -- the router could not score any model.
        if not model_id:
            raise ValueError("routing returned no model for this prompt")

        openrouter_model = self._resolve_model(model_id)

        # Build messages
        messages = []
        if system_message:
            messages.append({"role": "system", "content": system_message})
        messages.append({"role": "user", "content": prompt})

        payload = self._build_payload(openrouter_model, messages, temperature, max_tokens)

        # Async API call
        client = self._ensure_http_client()
        response = await self._post_with_retry(client, "/chat/completions", json=payload)
        response.raise_for_status()
        data = response.json()

        content = self._content_from_data(data)
        usage = data.get("usage", {})

        return OpenRouterResponse(
            content=content,
            model_used=model_id,
            openrouter_model=openrouter_model,
            route_reasoning=reasoning,
            usage=usage,
            raw_response=data,
        )

    async def stream(
        self,
        prompt: str,
        priorities: Optional[Priorities] = None,
        system_message: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> AsyncGenerator[str, None]:
        """
        Route the prompt to the best model and stream the response (async).

        Yields content chunks as they arrive from the API.

        Args:
            prompt: The user message to route and send.
            priorities: Override default priorities for this call.
            system_message: Optional system prompt.
            temperature: Sampling temperature (0.0 to 2.0).
            max_tokens: Maximum tokens in the response.

        Yields:
            String chunks of the response content.
        """
        prompt = self._validate_prompt(prompt)

        # Route in a thread (CPU-bound)
        route_result = await self.route(prompt, priorities=priorities)
        model_id = route_result.best_model

        # Never POST an empty model -- the router could not score any model.
        if not model_id:
            raise ValueError("routing returned no model for this prompt")

        openrouter_model = self._resolve_model(model_id)

        # Build messages
        messages = []
        if system_message:
            messages.append({"role": "system", "content": system_message})
        messages.append({"role": "user", "content": prompt})

        payload = self._build_payload(
            openrouter_model, messages, temperature, max_tokens, stream=True
        )

        # Async streaming with reconnect on transient errors. Retries only cover
        # the pre-first-byte connection phase: once a chunk has been yielded a
        # mid-stream failure must re-raise rather than replay (duplicating
        # already-emitted content).
        client = self._ensure_http_client()
        yielded_any = False
        last_exc: Exception | None = None
        for attempt in range(self._MAX_RETRIES + 1):
            try:
                async with client.stream(
                    "POST", "/chat/completions", json=payload
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                            if "error" in chunk:
                                err_msg = chunk["error"]
                                if isinstance(err_msg, dict):
                                    err_msg = err_msg.get("message", str(err_msg))
                                logger.error("Stream error from API: %s", err_msg)
                                raise ValueError(
                                    f"OpenRouter stream error: {err_msg}"
                                )
                            delta = chunk["choices"][0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                yielded_any = True
                                yield content
                        except (json.JSONDecodeError, KeyError, IndexError):
                            continue
                    return  # Stream completed successfully; exit retry loop
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                # Once content has started flowing, never retry from scratch.
                if yielded_any or attempt == self._MAX_RETRIES:
                    raise
                wait = 2 ** attempt + random.uniform(0, 1)
                logger.warning(
                    "Async stream connection failed (attempt %d/%d), retrying in %.1fs",
                    attempt + 1,
                    self._MAX_RETRIES + 1,
                    wait,
                )
                await asyncio.sleep(wait)

        raise last_exc  # type: ignore[misc]  # pragma: no cover

    async def route_and_chat(
        self,
        prompt: str,
        priorities: Optional[Priorities] = None,
        system_message: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> tuple[RouteResult, OpenRouterResponse]:
        """
        Route a prompt and get the AI response, returning both (async).

        Args:
            prompt: The user message to route and send.
            priorities: Override default priorities for this call.
            system_message: Optional system prompt.
            temperature: Sampling temperature (0.0 to 2.0).
            max_tokens: Maximum tokens in the response.

        Returns:
            Tuple of (RouteResult, OpenRouterResponse).
        """
        prompt = self._validate_prompt(prompt)

        # Route first
        route_result = await self.route(prompt, priorities=priorities)
        model_id = route_result.best_model
        reasoning = route_result.scores[0].reasoning if route_result.scores else ""

        # Never POST an empty model -- the router could not score any model.
        if not model_id:
            raise ValueError("routing returned no model for this prompt")

        openrouter_model = self._resolve_model(model_id)

        # Build messages
        messages = []
        if system_message:
            messages.append({"role": "system", "content": system_message})
        messages.append({"role": "user", "content": prompt})

        payload = self._build_payload(openrouter_model, messages, temperature, max_tokens)

        # Async API call
        client = self._ensure_http_client()
        response = await self._post_with_retry(client, "/chat/completions", json=payload)
        response.raise_for_status()
        data = response.json()

        content = self._content_from_data(data)
        usage = data.get("usage", {})

        api_response = OpenRouterResponse(
            content=content,
            model_used=model_id,
            openrouter_model=openrouter_model,
            route_reasoning=reasoning,
            usage=usage,
            raw_response=data,
        )

        return route_result, api_response

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def __aenter__(self) -> AsyncDREClient:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()
