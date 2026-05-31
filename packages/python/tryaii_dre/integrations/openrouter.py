"""
OpenRouter integration -- active routing through OpenRouter's API.

Wraps OpenRouter API calls so that TryAii-DRE automatically selects
the best model based on the prompt, then forwards the request.

Usage:
    from tryaii_dre import Router
    from tryaii_dre.integrations import OpenRouterIntegration

    router = Router()
    openrouter = OpenRouterIntegration(router, api_key="sk-or-...")

    # Auto-routes to best model and returns the response
    response = openrouter.chat("Write a Python quicksort implementation")
    print(response.model_used)   # e.g., "anthropic/claude-sonnet-4.5"
    print(response.content)      # The actual response text

    # Streaming
    for chunk in openrouter.stream("Explain quantum computing"):
        print(chunk, end="")
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import time
from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from tryaii_dre.classifiers.base import MAX_PROMPT_LENGTH

logger = logging.getLogger("tryaii_dre.integrations.openrouter")

# Mapping from our model IDs to OpenRouter model slugs
MODEL_ID_TO_OPENROUTER: dict[str, str] = {
    # OpenAI
    "gpt-4o": "openai/gpt-4o",
    "gpt-4o-mini": "openai/gpt-4o-mini",
    "o1": "openai/o1",
    "o3": "openai/o3",
    "o4-mini": "openai/o4-mini",
    "gpt-5": "openai/gpt-5",
    "gpt-5-mini": "openai/gpt-5-mini",
    "gpt-5.1": "openai/gpt-5.1",
    "gpt-5.2": "openai/gpt-5.2",
    "gpt-4.1": "openai/gpt-4.1",
    "gpt-4.1-nano": "openai/gpt-4.1-nano",
    "gpt-5-nano": "openai/gpt-5-nano",
    # Anthropic
    "claude-3-7-sonnet-20250219": "anthropic/claude-3.7-sonnet",
    "claude-sonnet-4-20250514": "anthropic/claude-sonnet-4",
    "claude-sonnet-4-5-20250929": "anthropic/claude-sonnet-4.5",
    "claude-haiku-4-5-20251001": "anthropic/claude-haiku-4.5",
    "claude-opus-4-5-20251101": "anthropic/claude-opus-4.5",
    # Google
    "gemini-2.5-pro": "google/gemini-2.5-pro",
    "gemini-2.0-flash": "google/gemini-2.0-flash",
    "gemini-2.5-flash": "google/gemini-2.5-flash",
    "gemini-2.5-flash-lite": "google/gemini-2.5-flash-lite",
    "gemini-3-pro-preview": "google/gemini-3-pro-preview",
    "gemini-3-flash-preview": "google/gemini-3-flash-preview",
    # DeepSeek
    "deepseek-reasoner": "deepseek/deepseek-reasoner",
    "deepseek-chat": "deepseek/deepseek-chat",
    # xAI
    "grok-3-latest": "x-ai/grok-3",
    "grok-3-mini-latest": "x-ai/grok-3-mini",
    "grok-4-latest": "x-ai/grok-4",
    "grok-4-fast": "x-ai/grok-4-fast",
    "grok-4-1-fast-reasoning-latest": "x-ai/grok-4.1-fast-reasoning",
    "grok-code-fast": "x-ai/grok-code-fast",
    # Mistral
    "mistral-large-latest": "mistralai/mistral-large",
    "mistral-small-latest": "mistralai/mistral-small",
}


@dataclass
class OpenRouterResponse:
    """Response from an OpenRouter API call."""

    content: str
    model_used: str  # TryAii-DRE model ID
    openrouter_model: str  # OpenRouter model slug
    route_reasoning: str  # Why this model was chosen
    usage: dict = field(default_factory=dict)
    raw_response: Optional[dict] = None


class OpenRouterIntegration:
    """
    Active routing integration with OpenRouter.

    Combines TryAii-DRE's semantic routing with OpenRouter's multi-provider
    API to automatically select and call the best model.
    """

    OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

    def __init__(
        self,
        router,  # tryaii_dre.Router instance (avoid circular import)
        api_key: Optional[str] = None,
        app_name: str = "tryaii-dre",
    ):
        self._router = router
        self._api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self._app_name = app_name
        self._client = None

    def _ensure_client(self):
        """Lazy-initialize httpx client."""
        if self._client is not None:
            return

        if not self._api_key:
            raise ValueError(
                "OpenRouter API key is required. Set OPENROUTER_API_KEY env var "
                "or pass api_key= to OpenRouterIntegration()"
            )

        try:
            import httpx
        except ImportError:
            raise ImportError(
                "httpx is required for OpenRouter integration. "
                "Install with: pip install tryaii-dre[openrouter]"
            )

        self._client = httpx.Client(
            base_url=self.OPENROUTER_BASE_URL,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "X-Title": self._app_name,
                "Content-Type": "application/json",
            },
            timeout=120.0,
        )

    def _resolve_model(self, model_id: str) -> str:
        """Convert TryAii-DRE model ID to OpenRouter slug."""
        return MODEL_ID_TO_OPENROUTER.get(model_id, model_id)

    # -- Retry helper --------------------------------------------------

    _RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
    _MAX_RETRIES = 3

    def _request_with_retry(self, method: str, url: str, **kwargs):
        """
        Make an HTTP request with exponential-backoff retry on transient errors.

        Retries on 429 (rate-limit) and 5xx server errors up to ``_MAX_RETRIES``
        times.  Honors the ``Retry-After`` header when present on 429 responses.
        """
        import httpx

        last_exc: Exception | None = None
        for attempt in range(self._MAX_RETRIES + 1):
            try:
                response = self._client.request(method, url, **kwargs)  # type: ignore[union-attr]
                if response.status_code not in self._RETRYABLE_STATUS_CODES:
                    return response
                # Retryable HTTP status -- fall through to backoff logic
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
                "Request to %s failed (attempt %d/%d), retrying in %.1fs",
                url,
                attempt + 1,
                self._MAX_RETRIES + 1,
                wait,
            )
            time.sleep(wait)

        # Should not be reached, but satisfy type checkers
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
                        # Naive HTTP-dates are UTC per RFC 7231.
                        if then.tzinfo is None:
                            then = then.replace(tzinfo=timezone.utc)
                        delta = (then - datetime.now(timezone.utc)).total_seconds()
                        return max(0.0, delta)
                except (ValueError, TypeError):
                    pass
        return 2 ** attempt + random.uniform(0, 1)

    # -- Resource cleanup ----------------------------------------------

    def close(self):
        """Close the underlying HTTP client and release resources."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # -- Public API -----------------------------------------------------

    def chat(
        self,
        prompt: str,
        priorities: Optional[dict] = None,
        system_message: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        override_model: Optional[str] = None,
    ) -> OpenRouterResponse:
        """
        Route and complete a chat request.

        Args:
            prompt: User message.
            priorities: Optional priority dict {"quality": 1-5, "cost": 1-5, "speed": 1-5}.
            system_message: Optional system prompt.
            temperature: Sampling temperature.
            max_tokens: Maximum response tokens.
            override_model: Skip routing and use this model directly.

        Returns:
            OpenRouterResponse with content and routing info.
        """
        if not prompt or not isinstance(prompt, str):
            raise ValueError("prompt must be a non-empty string")
        if len(prompt) > MAX_PROMPT_LENGTH:
            prompt = prompt[:MAX_PROMPT_LENGTH]

        self._ensure_client()

        # Route to best model (unless overridden)
        if override_model:
            model_id = override_model
            reasoning = f"Model override: {override_model}"
        else:
            from tryaii_dre.scoring.priorities import Priorities

            prio = Priorities.from_dict(priorities) if priorities else None
            route_result = self._router.route(prompt, priorities=prio)
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

        # Make API call
        payload: dict = {
            "model": openrouter_model,
            "messages": messages,
            "temperature": temperature,
        }
        # Only forward max_tokens when it is a positive, finite value.
        if max_tokens is not None and math.isfinite(max_tokens) and max_tokens > 0:
            payload["max_tokens"] = max_tokens

        response = self._request_with_retry("POST", "/chat/completions", json=payload)
        if response.status_code >= 400:
            logger.warning(
                "OpenRouter API error status=%d model=%s",
                response.status_code,
                openrouter_model,
            )
        response.raise_for_status()
        data = response.json()

        choices = data.get("choices")
        if not choices:
            # OpenRouter can return HTTP 200 with an {"error": ...} envelope and
            # no choices. 'error' may be a dict or a bare string.
            error = data.get("error")
            if isinstance(error, dict):
                msg = error.get("message", "No choices returned")
            elif isinstance(error, str):
                msg = error
            else:
                msg = "No choices returned"
            raise ValueError(f"OpenRouter API error: {msg}")
        content = choices[0].get("message", {}).get("content", "")
        usage = data.get("usage", {})

        logger.info("OpenRouter chat completed model=%s openrouter_model=%s tokens=%s",
                     model_id, openrouter_model, usage.get("total_tokens", "?"))

        return OpenRouterResponse(
            content=content,
            model_used=model_id,
            openrouter_model=openrouter_model,
            route_reasoning=reasoning,
            usage=usage,
            raw_response=data,
        )

    def stream(
        self,
        prompt: str,
        priorities: Optional[dict] = None,
        system_message: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        override_model: Optional[str] = None,
    ) -> Generator[str, None, None]:
        """
        Route and stream a chat response.

        Yields content chunks as they arrive.
        """
        if not prompt or not isinstance(prompt, str):
            raise ValueError("prompt must be a non-empty string")
        if len(prompt) > MAX_PROMPT_LENGTH:
            prompt = prompt[:MAX_PROMPT_LENGTH]

        self._ensure_client()

        # Route
        if override_model:
            model_id = override_model
        else:
            from tryaii_dre.scoring.priorities import Priorities

            prio = Priorities.from_dict(priorities) if priorities else None
            route_result = self._router.route(prompt, priorities=prio)
            model_id = route_result.best_model

        # Never POST an empty model -- the router could not score any model.
        if not model_id:
            raise ValueError("routing returned no model for this prompt")

        openrouter_model = self._resolve_model(model_id)

        logger.debug("OpenRouter stream started model=%s", openrouter_model)

        messages = []
        if system_message:
            messages.append({"role": "system", "content": system_message})
        messages.append({"role": "user", "content": prompt})

        payload: dict = {
            "model": openrouter_model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        # Only forward max_tokens when it is a positive, finite value.
        if max_tokens is not None and math.isfinite(max_tokens) and max_tokens > 0:
            payload["max_tokens"] = max_tokens

        import httpx

        # Retries only cover the pre-first-byte connection phase. Once a chunk
        # has been yielded a mid-stream failure must re-raise -- replaying the
        # request would duplicate already-emitted content.
        yielded_any = False
        last_exc: Exception | None = None
        for attempt in range(self._MAX_RETRIES + 1):
            try:
                with self._client.stream("POST", "/chat/completions", json=payload) as response:  # type: ignore[union-attr]
                    response.raise_for_status()
                    for line in response.iter_lines():
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
                                raise ValueError(f"OpenRouter stream error: {err_msg}")
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
                    "Stream connection failed (attempt %d/%d), retrying in %.1fs",
                    attempt + 1,
                    self._MAX_RETRIES + 1,
                    wait,
                )
                time.sleep(wait)

        raise last_exc  # type: ignore[misc]  # pragma: no cover
