"""
Model registry -- stores metadata about AI models.

Each model has benchmark scores, pricing, latency, and capabilities.
Ships with a default preset of 35+ models; users can add/remove/override.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional


@dataclass
class ModelPricing:
    """Pricing per 1k tokens in USD."""

    input_per_1k: float = 0.0
    output_per_1k: float = 0.0

    @property
    def average_per_1k(self) -> float:
        return (self.input_per_1k + self.output_per_1k) / 2


LatencyTier = Literal["very fast", "fast", "medium", "slow", "very slow"]


@dataclass
class ModelInfo:
    """
    Complete metadata for a single AI model.

    This is the unit that gets scored by the ScoringEngine.
    """

    model_id: str
    provider: str
    benchmark_scores: dict[str, Optional[float]] = field(default_factory=dict)
    capabilities: list[str] = field(default_factory=list)
    pricing: Optional[ModelPricing] = None
    latency: Optional[LatencyTier] = None
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "provider": self.provider,
            "benchmark_scores": self.benchmark_scores,
            "capabilities": self.capabilities,
            "pricing": {
                "input_per_1k": self.pricing.input_per_1k,
                "output_per_1k": self.pricing.output_per_1k,
            }
            if self.pricing
            else None,
            "latency": self.latency,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ModelInfo:
        pricing = None
        if d.get("pricing"):
            pricing = ModelPricing(
                input_per_1k=d["pricing"].get("input_per_1k", 0),
                output_per_1k=d["pricing"].get("output_per_1k", 0),
            )

        # Filter out null benchmark scores
        benchmark_scores = {
            k: v for k, v in (d.get("benchmark_scores") or {}).items() if v is not None
        }
        return cls(
            model_id=d["model_id"],
            provider=d["provider"],
            benchmark_scores=benchmark_scores,
            capabilities=d.get("capabilities", []),
            pricing=pricing,
            latency=d.get("latency"),
            description=d.get("description", ""),
        )


class ModelRegistry:
    """
    Registry of AI models and their metadata.

    Usage:
        registry = ModelRegistry()                  # Empty registry
        registry = ModelRegistry.default()           # Pre-loaded with 35+ models
        registry.add_model(ModelInfo(...))           # Add a model
        registry.remove_model("gpt-4o")             # Remove a model
        models = registry.filter(provider="OpenAI")  # Filter models
    """

    def __init__(self):
        self._models: dict[str, ModelInfo] = {}

    @classmethod
    def default(cls) -> ModelRegistry:
        """Create a registry pre-loaded with the default model preset."""
        registry = cls()
        registry.load_preset("default")
        return registry

    def add_model(self, model: ModelInfo) -> None:
        """Add or update a model in the registry."""
        self._models[model.model_id] = model

    def add(
        self,
        model_id: str,
        provider: str,
        benchmarks: Optional[dict[str, float]] = None,
        pricing: Optional[tuple[float, float]] = None,
        latency: Optional[LatencyTier] = None,
        capabilities: Optional[list[str]] = None,
        description: str = "",
    ) -> ModelInfo:
        """
        Convenience method to add a model with keyword arguments.

        Args:
            model_id: Unique model identifier.
            provider: Provider name (e.g., "OpenAI", "Anthropic").
            benchmarks: Dict of benchmark_name -> score.
            pricing: Tuple of (input_cost_per_1k, output_cost_per_1k) in USD.
            latency: Latency tier string.
            capabilities: List of capability strings.
            description: Human-readable description.

        Returns:
            The created ModelInfo.
        """
        model_pricing = None
        if pricing:
            model_pricing = ModelPricing(
                input_per_1k=pricing[0], output_per_1k=pricing[1]
            )

        model = ModelInfo(
            model_id=model_id,
            provider=provider,
            benchmark_scores=benchmarks or {},
            pricing=model_pricing,
            latency=latency,
            capabilities=capabilities or [],
            description=description,
        )
        self.add_model(model)
        return model

    def remove_model(self, model_id: str) -> bool:
        """Remove a model from the registry. Returns True if removed."""
        return self._models.pop(model_id, None) is not None

    def get_model(self, model_id: str) -> Optional[ModelInfo]:
        """Get a model by ID."""
        return self._models.get(model_id)

    def filter(
        self,
        provider: Optional[str] = None,
        capability: Optional[str] = None,
        max_input_cost: Optional[float] = None,
        latency: Optional[LatencyTier] = None,
    ) -> list[ModelInfo]:
        """Filter models by criteria."""
        results = list(self._models.values())

        if provider:
            provider_lower = provider.lower()
            results = [m for m in results if m.provider.lower() == provider_lower]

        if capability:
            results = [m for m in results if capability in m.capabilities]

        if max_input_cost is not None:
            results = [
                m
                for m in results
                if m.pricing and m.pricing.input_per_1k <= max_input_cost
            ]

        if latency:
            results = [m for m in results if m.latency == latency]

        return results

    @property
    def all_models(self) -> list[ModelInfo]:
        """All registered models."""
        return list(self._models.values())

    @property
    def model_ids(self) -> list[str]:
        """All registered model IDs."""
        return list(self._models.keys())

    def __len__(self) -> int:
        return len(self._models)

    def __contains__(self, model_id: str) -> bool:
        return model_id in self._models

    def load_preset(self, name: str = "default") -> int:
        """
        Load a preset model registry from bundled JSON.

        Args:
            name: Preset name ("default" ships with the package).

        Returns:
            Number of models loaded.
        """
        preset_dir = Path(__file__).parent / "presets"
        preset_file = preset_dir / f"{name}_models.json"

        if not preset_file.exists():
            raise FileNotFoundError(f"Preset '{name}' not found at {preset_file}")

        with open(preset_file) as f:
            data = json.load(f)

        count = 0
        for model_data in data.get("models", []):
            model = ModelInfo.from_dict(model_data)
            self.add_model(model)
            count += 1

        return count

    def export_json(self, path: str | Path) -> None:
        """Export registry to JSON file."""
        path = Path(path)
        data = {
            "models": [m.to_dict() for m in self._models.values()]
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
