"""
Extensible benchmark registry.

Allows users to register custom benchmarks with their own training queries
and normalization ranges. Designed for high connectivity with external
benchmark-creation tools.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from tryaii_dre.scoring.benchmarks import BenchmarkNormalizer, NormalizationRange


@dataclass
class BenchmarkDefinition:
    """
    Complete definition of a benchmark.

    Contains everything needed to integrate a new benchmark
    into the routing system.
    """

    name: str
    description: str
    training_queries: list[str]
    normalization: NormalizationRange
    broad_category: str = "TECHNICAL"
    subcategories: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "training_queries": self.training_queries,
            "normalization": {
                "min_score": self.normalization.min_score,
                "max_score": self.normalization.max_score,
            },
            "broad_category": self.broad_category,
            "subcategories": self.subcategories,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict) -> BenchmarkDefinition:
        norm = d.get("normalization", {})
        return cls(
            name=d["name"],
            description=d.get("description", ""),
            training_queries=d.get("training_queries", []),
            normalization=NormalizationRange(
                min_score=norm.get("min_score", 0),
                max_score=norm.get("max_score", 100),
            ),
            broad_category=d.get("broad_category", "TECHNICAL"),
            subcategories=d.get("subcategories", []),
            metadata=d.get("metadata", {}),
        )


class BenchmarkRegistry:
    """
    Registry for benchmark definitions.

    Provides a clean interface for:
        - Registering custom benchmarks
        - Loading benchmarks from JSON files (for tool connectivity)
        - Exporting benchmark definitions
        - Integrating with the centroid generator and scoring engine

    Usage:
        registry = BenchmarkRegistry.default()  # Standard 12 benchmarks

        # Add a custom benchmark
        registry.register(BenchmarkDefinition(
            name="CustomerSupportQA",
            description="Customer support query handling quality",
            training_queries=[
                "How do I reset my password?",
                "I want to cancel my subscription",
                "Where is my order?",
                ...
            ],
            normalization=NormalizationRange(0, 100),
            broad_category="CONVERSATIONAL",
        ))

        # Load from external tool output
        registry.load_from_file("my_benchmarks.json")
    """

    def __init__(self):
        self._benchmarks: dict[str, BenchmarkDefinition] = {}

    @classmethod
    def default(cls) -> BenchmarkRegistry:
        """Create registry with the standard 12 benchmarks."""
        from tryaii_dre.benchmarks.standard import STANDARD_BENCHMARKS

        registry = cls()
        for benchmark in STANDARD_BENCHMARKS:
            registry._benchmarks[benchmark.name] = benchmark
        return registry

    def register(self, benchmark: BenchmarkDefinition) -> None:
        """Register a new benchmark or update an existing one."""
        self._benchmarks[benchmark.name] = benchmark

    def unregister(self, name: str) -> bool:
        """Remove a benchmark. Returns True if it existed."""
        return self._benchmarks.pop(name, None) is not None

    def get(self, name: str) -> Optional[BenchmarkDefinition]:
        """Get a benchmark by name."""
        return self._benchmarks.get(name)

    @property
    def names(self) -> list[str]:
        """All registered benchmark names."""
        return list(self._benchmarks.keys())

    @property
    def all_benchmarks(self) -> list[BenchmarkDefinition]:
        """All registered benchmarks."""
        return list(self._benchmarks.values())

    def get_training_queries(self) -> dict[str, list[str]]:
        """Get all training queries grouped by benchmark name."""
        return {
            name: b.training_queries
            for name, b in self._benchmarks.items()
            if b.training_queries
        }

    def get_normalizer(self) -> BenchmarkNormalizer:
        """Create a BenchmarkNormalizer from all registered benchmarks."""
        normalizer = BenchmarkNormalizer()
        for name, benchmark in self._benchmarks.items():
            normalizer.register_range(
                name,
                benchmark.normalization.min_score,
                benchmark.normalization.max_score,
                benchmark.description,
            )
        return normalizer

    def load_from_file(self, path: str | Path) -> int:
        """
        Load benchmarks from a JSON file.

        Returns:
            Number of benchmarks loaded.
        """
        with open(path) as f:
            data = json.load(f)

        count = 0
        for item in data.get("benchmarks", []):
            benchmark = BenchmarkDefinition.from_dict(item)
            self.register(benchmark)
            count += 1

        return count

    def export_to_file(self, path: str | Path) -> None:
        """Export all benchmarks to a JSON file."""
        path = Path(path)
        data = {
            "benchmarks": [b.to_dict() for b in self._benchmarks.values()]
        }
        # Write to temp file then atomically rename
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, path)
        except BaseException:
            os.unlink(tmp_path)
            raise

    def __len__(self) -> int:
        return len(self._benchmarks)

    def __contains__(self, name: str) -> bool:
        return name in self._benchmarks
