"""
TryAii-DRE -- Embedding-based AI Model Router

Understands your prompt semantically and routes to the best model
based on benchmarks, cost, speed, and quality priorities.

Usage:
    from tryaii_dre import Router

    router = Router()
    result = router.route("Write a Python function to merge sorted arrays")
    print(result.best_model)
    print(result.scores)
"""

import logging

from tryaii_dre.benchmarks.registry import BenchmarkRegistry
from tryaii_dre.budget import (
    DEFAULT_DIFFICULTY_GAMMA,
    DEFAULT_DIFFICULTY_SOURCE,
    BudgetCandidate,
    BudgetedRouteResult,
    BudgetOptimizationResult,
    compute_difficulty,
    estimate_tokens,
    route_dataset_with_budget,
)
from tryaii_dre.client import DREClient
from tryaii_dre.config import TryaiiDreConfig
from tryaii_dre.registry.models import ModelInfo, ModelRegistry
from tryaii_dre.router import Router, RouteResult
from tryaii_dre.scoring.priorities import DEFAULT_PRIORITIES, Priorities

# Attach a NullHandler so library logging stays silent unless the host app
# configures handlers. Done after imports to keep module-level imports at top.
logging.getLogger("tryaii_dre").addHandler(logging.NullHandler())

__version__ = "0.2.2"

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
        from tryaii_dre.async_client import AsyncDREClient

        return AsyncDREClient
    raise AttributeError(f"module 'tryaii_dre' has no attribute {name!r}")
