"""Tests for the model registry."""

import json
import tempfile
from pathlib import Path

from tryaii.registry.models import ModelInfo, ModelPricing, ModelRegistry


class TestModelInfo:
    def test_from_dict_roundtrip(self):
        model = ModelInfo(
            model_id="test-model",
            provider="TestProvider",
            benchmark_scores={"HumanEval": 90.0, "MMLU": 85.0},
            capabilities=["code-generation"],
            pricing=ModelPricing(input_per_1k=0.002, output_per_1k=0.008),
            latency="fast",
            description="A test model",
        )
        d = model.to_dict()
        restored = ModelInfo.from_dict(d)

        assert restored.model_id == model.model_id
        assert restored.provider == model.provider
        assert restored.benchmark_scores == model.benchmark_scores
        assert restored.pricing.input_per_1k == model.pricing.input_per_1k
        assert restored.latency == model.latency

    def test_from_dict_no_pricing(self):
        d = {"model_id": "test", "provider": "Test", "benchmark_scores": {}}
        model = ModelInfo.from_dict(d)
        assert model.pricing is None


class TestModelPricing:
    def test_average_cost(self):
        p = ModelPricing(input_per_1k=0.002, output_per_1k=0.008)
        assert abs(p.average_per_1k - 0.005) < 1e-6


class TestModelRegistry:
    def test_default_loads_models(self):
        registry = ModelRegistry.default()
        assert len(registry) > 30
        assert "gpt-4o" in registry
        assert "claude-opus-4-5-20251101" in registry

    def test_add_and_get_model(self):
        registry = ModelRegistry()
        model = ModelInfo(model_id="test", provider="Test")
        registry.add_model(model)

        assert "test" in registry
        assert registry.get_model("test").provider == "Test"

    def test_add_convenience_method(self):
        registry = ModelRegistry()
        model = registry.add(
            "my-model",
            provider="Custom",
            benchmarks={"HumanEval": 88.0},
            pricing=(0.001, 0.004),
            latency="fast",
        )

        assert model.model_id == "my-model"
        assert registry.get_model("my-model").pricing.input_per_1k == 0.001

    def test_remove_model(self):
        registry = ModelRegistry()
        registry.add("test", provider="Test")
        assert registry.remove_model("test") is True
        assert "test" not in registry
        assert registry.remove_model("nonexistent") is False

    def test_filter_by_provider(self):
        registry = ModelRegistry.default()
        anthropic = registry.filter(provider="Anthropic")
        assert all(m.provider == "Anthropic" for m in anthropic)
        assert len(anthropic) >= 3

    def test_filter_by_capability(self):
        registry = ModelRegistry.default()
        coders = registry.filter(capability="code-generation")
        assert all("code-generation" in m.capabilities for m in coders)

    def test_filter_by_max_cost(self):
        registry = ModelRegistry.default()
        cheap = registry.filter(max_input_cost=0.001)
        assert all(m.pricing.input_per_1k <= 0.001 for m in cheap if m.pricing)

    def test_model_ids_property(self):
        registry = ModelRegistry.default()
        ids = registry.model_ids
        assert isinstance(ids, list)
        assert "gpt-4o" in ids

    def test_export_import_json(self):
        registry = ModelRegistry()
        registry.add("m1", provider="A", benchmarks={"MMLU": 80.0})
        registry.add("m2", provider="B", benchmarks={"HumanEval": 90.0})

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name

        registry.export_json(path)

        # Load into new registry
        new_registry = ModelRegistry()
        with open(path) as f:
            data = json.load(f)
        for m in data["models"]:
            new_registry.add_model(ModelInfo.from_dict(m))

        assert len(new_registry) == 2
        assert "m1" in new_registry
        assert "m2" in new_registry

        Path(path).unlink()

    def test_default_preset_has_all_providers(self):
        registry = ModelRegistry.default()
        providers = {m.provider for m in registry.all_models}
        assert "OpenAI" in providers
        assert "Anthropic" in providers
        assert "Google" in providers
        assert "DeepSeek" in providers
        assert "xAI" in providers
        assert "Mistral" in providers
