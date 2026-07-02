"""
TryAii-DRE -- Embedding-based AI Model Router

Understands your prompt semantically and routes to the best model
based on benchmarks, cost, speed, and quality priorities.

Usage:
    from tryaii import Router

    router = Router()
    result = router.route("Write a Python function to merge sorted arrays")
    print(result.best_model)
    print(result.scores)
"""

import logging

from tryaii.benchmarks.registry import BenchmarkRegistry
from tryaii.budget import (
    DEFAULT_DIFFICULTY_GAMMA,
    DEFAULT_DIFFICULTY_SOURCE,
    BudgetCandidate,
    BudgetedRouteResult,
    BudgetOptimizationResult,
    compute_difficulty,
    estimate_tokens,
    route_dataset_with_budget,
)
from tryaii.client import DREClient
from tryaii.config import TryaiiDreConfig
from tryaii.registry.models import ModelInfo, ModelRegistry
from tryaii.router import Router, RouteResult
from tryaii.scoring.priorities import DEFAULT_PRIORITIES, Priorities

# Attach a NullHandler so library logging stays silent unless the host app
# configures handlers. Done after imports to keep module-level imports at top.
logging.getLogger("tryaii").addHandler(logging.NullHandler())

__version__ = "0.4.0"

__all__ = [
    "Router",
    "RouteResult",
    "ModelRegistry",
    "ModelInfo",
    "Priorities",
    "DEFAULT_PRIORITIES",
    "BenchmarkRegistry",
    "TryaiiDreConfig",
    "DREClient",
    "AsyncDREClient",
    "BudgetCandidate",
    "BudgetOptimizationResult",
    "BudgetedRouteResult",
    "DEFAULT_DIFFICULTY_GAMMA",
    "DEFAULT_DIFFICULTY_SOURCE",
    "compute_difficulty",
    "estimate_tokens",
    "route_dataset_with_budget",
    "__version__",
]


def __getattr__(name: str):
    if name == "AsyncDREClient":
        from tryaii.async_client import AsyncDREClient

        return AsyncDREClient
    raise AttributeError(f"module 'tryaii' has no attribute {name!r}")
