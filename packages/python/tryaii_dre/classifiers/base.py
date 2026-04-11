"""
Abstract base classifier.

The embedding classifier implements this interface. The abstraction is
kept so custom classifiers can be plugged in for tests or research.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

# Maximum allowed prompt length (characters).  Prompts longer than this are
# silently truncated to avoid OOM in the embedding model.
MAX_PROMPT_LENGTH: int = 100_000


@dataclass
class ClassificationResult:
    """
    Output of any classifier.

    Contains benchmark similarity scores and category information.
    This is what the ScoringEngine consumes to rank models.
    """

    # Core: cosine similarity to each benchmark centroid (0-1)
    benchmark_scores: dict[str, float] = field(default_factory=dict)

    # Category info (for display / debugging)
    broad_category: str = ""
    subcategory: str = ""
    confidence: float = 0.0

    # Metadata
    classifier_used: str = ""  # always "embedding" in the current system
    cache_hit: bool = False
    processing_time_ms: float = 0.0

    @property
    def top_benchmarks(self) -> list[tuple[str, float]]:
        """Top benchmarks sorted by similarity score."""
        return sorted(
            self.benchmark_scores.items(), key=lambda x: x[1], reverse=True
        )


class BaseClassifier(ABC):
    """
    Abstract base class for prompt classifiers.

    A classifier takes a user prompt and returns benchmark similarity scores.
    These scores tell us "what kind of task is this?" in terms of which
    AI benchmarks it most resembles.
    """

    @abstractmethod
    def classify(self, prompt: str) -> ClassificationResult:
        """
        Classify a prompt and return benchmark similarity scores.

        Args:
            prompt: The user's input text.

        Returns:
            ClassificationResult with benchmark_scores populated.
        """
        ...

    @abstractmethod
    def is_ready(self) -> bool:
        """Check if the classifier is initialized and ready to use."""
        ...
