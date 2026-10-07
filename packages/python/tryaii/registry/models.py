"""
Model registry -- stores metadata about AI models.

Each model has benchmark scores, pricing, latency, and capabilities.
The default registry is the active catalog bundle's ``models.json`` (the
packaged starter catalog unless another bundle is given -- see
:mod:`tryaii.catalog`); users can add/remove/override.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Optional

if TYPE_CHECKING:
    from tryaii.catalog.bundle import CatalogBundle


@dataclass
class ModelPricing:
    """Pricing per 1k tokens in USD."""

    input_per_1k: float = 0.0
    output_per_1k: float = 0.0

    @property
    def average_per_1k(self) -> float:
        return (self.input_per_1k + self.output_per_1k) / 2


# "unknown" is shipped for catalog models whose provider publishes no speed
# data; the scoring engine falls back to the catalog p25 of the speed utility.
LatencyTier = Literal["very fast", "fast", "medium", "slow", "very slow", "unknown"]

# OpenRouter marks ephemeral free-tier variants with a ":free" id suffix.
FREE_TIER_SUFFIX = ":free"


def is_free_tier(model_id: str) -> bool:
    """True for ephemeral free-tier model variants (OpenRouter ``:free`` ids).

    Free tiers come and go -- often within days -- so regular routing ignores
    them entirely: they are excluded from the default registry and never
    scored. The preset data still ships them so a future dedicated free-models
    feature can opt in via ``load_preset(..., include_free=True)``.
    """
    return model_id.endswith(FREE_TIER_SUFFIX)


def _parse_tokens_per_second(value: object) -> Optional[float]:
    """Accept an int/float tokens-per-second value; anything else is unknown.

    Non-numeric, non-finite, and non-positive values are treated as "no
    measurement" (None) rather than as a slow model. bool is rejected because
    it is an int subclass in Python.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    tps = float(value)
    if not math.isfinite(tps) or tps <= 0:
        return None
    return tps


def _parse_ttft_ms(value: object) -> Optional[float]:
    """Accept an int/float time-to-first-token (ms) value; anything else is unknown.

    Non-numeric, non-finite, and non-positive values are treated as "no
    measurement" (None) rather than as an instant first token. bool is rejected
    because it is an int subclass in Python.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    ttft = float(value)
    if not math.isfinite(ttft) or ttft <= 0:
        return None
    return ttft


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
    # Measured output speed (fastest provider), tokens/second. When present the
    # scoring engine uses it for a continuous speed score; ``latency`` stays
    # the display tier and the fallback. None = no measurement.
    tokens_per_second: Optional[float] = None
    # Measured time to first token (fastest provider), milliseconds. Pairs with
    # ``tokens_per_second`` to give the whole latency of an answer.
    # None = no measurement.
    ttft_ms: Optional[float] = None

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
            "tokens_per_second": self.tokens_per_second,
            "ttft_ms": self.ttft_ms,
            "description": self.description,
        }

    @classmethod
    def from_dict(
        cls, d: dict, random_chance_floors: Optional[dict[str, float]] = None
    ) -> ModelInfo:
        # Pricing with a missing component is unknown, not free: coercing null
        # to 0 would hand the model a perfect cost score.
        pricing = None
        raw_pricing = d.get("pricing") or {}
        if (
            raw_pricing.get("input_per_1k") is not None
            and raw_pricing.get("output_per_1k") is not None
        ):
            pricing = ModelPricing(
                input_per_1k=raw_pricing["input_per_1k"],
                output_per_1k=raw_pricing["output_per_1k"],
            )

        # Drop null and implausible (corrupt) benchmark values such as
        # below-random-chance multiple-choice scores -- keeping them would both
        # crater the model and poison the registry-wide imputation medians.
        from tryaii.scoring.benchmarks import is_implausible_benchmark_score

        # ``random_chance_floors`` are the catalog's (benchmarks.json); None
        # means the packaged starter catalog's floors.
        benchmark_scores = {
            k: v
            for k, v in (d.get("benchmark_scores") or {}).items()
            if v is not None
            and not is_implausible_benchmark_score(k, v, random_chance_floors)
        }
        return cls(
            model_id=d["model_id"],
            provider=d["provider"],
            benchmark_scores=benchmark_scores,
            capabilities=d.get("capabilities", []),
            pricing=pricing,
            latency=d.get("latency"),
            description=d.get("description", ""),
            tokens_per_second=_parse_tokens_per_second(d.get("tokens_per_second")),
            ttft_ms=_parse_ttft_ms(d.get("ttft_ms")),
        )


def compute_benchmark_coverage(models: list[ModelInfo]) -> dict[str, float]:
    """Per-benchmark reporting coverage over the *routable* subset of ``models``.

    ``coverage(b) = n_real(b) / N`` where ``N`` is the number of routable models
    (``:free`` ids excluded -- the same set the Router routes over) and
    ``n_real(b)`` how many of them carry a real, finite
    ``benchmark_scores[b]``. The scoring engine multiplies every per-benchmark
    term by this, so a benchmark only ~15% of the catalog reports cannot carry
    the same weight as one 73% of it reports.

    Benchmarks no routable model reports are simply absent from the result; read
    it with ``.get(name, 0.0)``, which is the defined coverage for them. Must
    stay behaviour-identical with ``computeBenchmarkCoverage`` in the Node SDK.
    """
    routable = [m for m in models if not is_free_tier(m.model_id)]
    total = len(routable)
    if total == 0:
        return {}
    counts: dict[str, int] = {}
    for model in routable:
        for name, raw in model.benchmark_scores.items():
            if raw is not None and math.isfinite(raw):
                counts[name] = counts.get(name, 0) + 1
    return {name: count / total for name, count in counts.items()}


class ModelRegistry:
    """
    Registry of AI models and their metadata.

    Usage:
        registry = ModelRegistry()                  # Empty registry
        registry = ModelRegistry.default()           # default catalog's routable models
        registry = ModelRegistry.from_bundle(bundle) # one catalog bundle's models
        registry.add_model(ModelInfo(...))           # Add a model
        registry.remove_model("gpt-4o")             # Remove a model
        models = registry.filter(provider="OpenAI")  # Filter models
    """

    def __init__(self):
        self._models: dict[str, ModelInfo] = {}
        # Memoised :func:`compute_benchmark_coverage` over the current contents;
        # dropped by every mutation so an added/removed model re-derives it.
        self._coverage_cache: Optional[dict[str, float]] = None

    @classmethod
    def default(cls, include_free: bool = False, bundle=None,
                catalog: str = "auto") -> ModelRegistry:
        """Create a registry pre-loaded with the default catalog's models.

        Ephemeral free-tier variants (``:free`` ids) are excluded unless
        ``include_free=True`` -- see :func:`is_free_tier`. ``bundle`` (a
        CatalogBundle or bundle directory) overrides the default catalog -- see
        :func:`tryaii.catalog.resolve_bundle`.
        """
        from tryaii.catalog.bundle import resolve_bundle

        return cls.from_bundle(resolve_bundle(bundle, catalog=catalog),
                               include_free=include_free)

    @classmethod
    def from_bundle(cls, bundle: CatalogBundle, include_free: bool = False) -> ModelRegistry:
        """Create a registry holding one catalog bundle's models."""
        registry = cls()
        registry.load_from_bundle(bundle, include_free=include_free)
        return registry

    def load_from_bundle(self, bundle: CatalogBundle, include_free: bool = False) -> int:
        """Add a catalog bundle's models (its own random-chance floors apply).

        Returns the number of models loaded.
        """
        floors = bundle.random_chance_floors()
        count = 0
        for model_data in bundle.model_entries():
            if not include_free and is_free_tier(model_data.get("model_id", "")):
                continue
            self.add_model(ModelInfo.from_dict(model_data, random_chance_floors=floors))
            count += 1
        return count

    def add_model(self, model: ModelInfo) -> None:
        """Add or update a model in the registry."""
        self._models[model.model_id] = model
        self._coverage_cache = None

    def benchmark_coverage(self) -> dict[str, float]:
        """Per-benchmark reporting coverage of this registry (cached).

        See :func:`compute_benchmark_coverage`. The result is recomputed
        whenever a model is added or removed, so it always describes the live
        catalog; a benchmark absent from the mapping has coverage ``0.0``.
        """
        if self._coverage_cache is None:
            self._coverage_cache = compute_benchmark_coverage(self.all_models)
        return self._coverage_cache

    def add(
        self,
        model_id: str,
        provider: str,
        benchmarks: Optional[dict[str, float]] = None,
        pricing: Optional[tuple[float, float]] = None,
        latency: Optional[LatencyTier] = None,
        capabilities: Optional[list[str]] = None,
        description: str = "",
        tokens_per_second: Optional[float] = None,
        ttft_ms: Optional[float] = None,
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
            tokens_per_second: Measured output speed (fastest provider); used
                for the continuous speed score when given.
            ttft_ms: Measured time to first token in ms (fastest provider);
                used for the continuous speed score when given.

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
            tokens_per_second=_parse_tokens_per_second(tokens_per_second),
            ttft_ms=_parse_ttft_ms(ttft_ms),
        )
        self.add_model(model)
        return model

    def remove_model(self, model_id: str) -> bool:
        """Remove a model from the registry. Returns True if removed."""
        removed = self._models.pop(model_id, None) is not None
        if removed:
            self._coverage_cache = None
        return removed

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

    def load_preset(self, name: str = "default", include_free: bool = False) -> int:
        """
        Load a preset model set. Kept for backwards compatibility.

        Args:
            name: Preset name. Only "default" exists: the default catalog
                bundle's models (the packaged starter catalog).
            include_free: Also load ephemeral free-tier variants (``:free``
                ids). They are skipped by default because free tiers come and
                go too quickly to route production traffic against.

        Returns:
            Number of models loaded.
        """
        if name != "default":
            raise FileNotFoundError(
                f"Preset '{name}' not found (only 'default' -- the default catalog -- exists)"
            )
        from tryaii.catalog.bundle import resolve_bundle

        return self.load_from_bundle(resolve_bundle(None), include_free=include_free)

    def export_json(self, path: str | Path) -> None:
        """Export registry to JSON file."""
        path = Path(path)
        data = {
            "models": [m.to_dict() for m in self._models.values()]
        }
        # Write to temp file then atomically rename
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, path)
        except BaseException:
            os.unlink(tmp_path)
            raise
