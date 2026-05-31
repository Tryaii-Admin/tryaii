"""
DREClient -- unified high-level client for TryAii-DRE.

Wraps Router + OpenRouterIntegration into a single class so users
do not need to manage two separate objects.

Usage:
    from tryaii_dre import DREClient

    client = DREClient(api_key="sk-or-...")
    response = client.chat("Write a quicksort in Python")
    print(response.model_used, response.content)
"""

from __future__ import annotations

import os
from collections.abc import Generator
from typing import Optional

from tryaii_dre.config import TryaiiDreConfig
from tryaii_dre.integrations.openrouter import OpenRouterIntegration, OpenRouterResponse
from tryaii_dre.router import Router, RouteResult
from tryaii_dre.scoring.priorities import Priorities


class DREClient:
    """
    High-level client that combines routing and API calls.

    Creates a Router and OpenRouterIntegration internally so you only
    need a single object to route prompts and get AI responses.

    Args:
        api_key: OpenRouter API key. Falls back to OPENROUTER_API_KEY env var.
        priorities: Default priorities for all routing calls. Can be
                    overridden per-request.
        embedding_model: Sentence-transformers model name for embeddings.
                         Defaults to "all-MiniLM-L6-v2".
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

        # Core router
        self._router = Router(config=config)

        # OpenRouter integration for API calls
        self._openrouter = OpenRouterIntegration(
            router=self._router,
            api_key=self._api_key,
        )

    @property
    def router(self) -> Router:
        """Access the underlying Router instance."""
        return self._router

    @property
    def openrouter(self) -> OpenRouterIntegration:
        """Access the underlying OpenRouterIntegration instance."""
        return self._openrouter

    def chat(
        self,
        prompt: str,
        priorities: Optional[Priorities] = None,
        system_message: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> OpenRouterResponse:
        """
        Route the prompt to the best model and return the AI response.

        Args:
            prompt: The user message to route and send.
            priorities: Override default priorities for this call.
            system_message: Optional system prompt.
            temperature: Sampling temperature (0.0 to 2.0).
            max_tokens: Maximum tokens in the response.

        Returns:
            OpenRouterResponse with content, model_used, and routing info.
        """
        prio = priorities or self._default_priorities
        prio_dict = prio.to_dict() if prio else None

        return self._openrouter.chat(
            prompt=prompt,
            priorities=prio_dict,
            system_message=system_message,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def stream(
        self,
        prompt: str,
        priorities: Optional[Priorities] = None,
        system_message: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> Generator[str, None, None]:
        """
        Route the prompt to the best model and stream the response.

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
        prio = priorities or self._default_priorities
        prio_dict = prio.to_dict() if prio else None

        yield from self._openrouter.stream(
            prompt=prompt,
            priorities=prio_dict,
            system_message=system_message,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def route(
        self,
        prompt: str,
        priorities: Optional[Priorities] = None,
        top_k: int = 5,
    ) -> RouteResult:
        """
        Route a prompt without making an API call.

        Useful for inspecting which model would be selected and why,
        without incurring any API cost.

        Args:
            prompt: The user message to classify and route.
            priorities: Override default priorities for this call.
            top_k: Number of top models to include in results.

        Returns:
            RouteResult with best_model, scores, and classification details.
        """
        prio = priorities or self._default_priorities
        return self._router.route(prompt, priorities=prio, top_k=top_k)

    def route_and_chat(
        self,
        prompt: str,
        priorities: Optional[Priorities] = None,
        system_message: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> tuple[RouteResult, OpenRouterResponse]:
        """
        Route a prompt and get the AI response, returning both.

        This is useful when you want to display or log the routing decision
        alongside the actual response.

        Args:
            prompt: The user message to route and send.
            priorities: Override default priorities for this call.
            system_message: Optional system prompt.
            temperature: Sampling temperature (0.0 to 2.0).
            max_tokens: Maximum tokens in the response.

        Returns:
            Tuple of (RouteResult, OpenRouterResponse).
        """
        prio = priorities or self._default_priorities

        # Step 1: Route to get the decision
        route_result = self._router.route(
            prompt, priorities=prio, top_k=5
        )

        # Step 2: Call the API with the chosen model
        prio_dict = prio.to_dict() if prio else None
        response = self._openrouter.chat(
            prompt=prompt,
            priorities=prio_dict,
            system_message=system_message,
            temperature=temperature,
            max_tokens=max_tokens,
            override_model=route_result.best_model,
        )

        return route_result, response
