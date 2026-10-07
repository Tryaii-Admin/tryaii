"""Free-tier model exclusion.

Free tiers (OpenRouter ``:free`` id variants) come and go -- often within
days -- so the product must not route regular traffic against them. The rule:
regular usage ignores them completely, but the full catalog keeps them so a
future dedicated free-models feature can opt in. The packaged starter catalog
ships no free tiers at all; the counts below are pinned on the full catalog
bundle (skipped when it has not been built).

Mirrors packages/node/tests/registry/freeTier.test.ts.
"""

from __future__ import annotations

import pytest

from tryaii.catalog import starter_bundle
from tryaii.registry.models import ModelRegistry, is_free_tier


@pytest.fixture
def raw_models(full_bundle) -> list[dict]:
    return full_bundle.model_entries()


class TestIsFreeTier:
    def test_free_suffix_detected(self):
        assert is_free_tier("openai/gpt-oss-120b:free")
        assert is_free_tier("z-ai/glm-4.5-air:free")

    def test_regular_ids_are_not_free(self):
        assert not is_free_tier("openai/gpt-5.5")
        assert not is_free_tier("anthropic/claude-fable-5")

    def test_free_must_be_a_suffix_not_a_substring(self):
        assert not is_free_tier("freellm/model")
        assert not is_free_tier("vendor/free-model")


class TestDefaultRegistryExcludesFree:
    def test_no_free_models_in_default_registry(self):
        registry = ModelRegistry.default()
        free = [i for i in registry.model_ids if is_free_tier(i)]
        assert free == [], f"free-tier models leaked into regular usage: {free[:5]}"

    def test_starter_catalog_ships_no_free_tiers(self):
        assert not any(is_free_tier(m["model_id"]) for m in starter_bundle().model_entries())

    def test_no_free_models_in_full_registry(self, full_bundle):
        registry = ModelRegistry.default(bundle=full_bundle)
        assert not any(is_free_tier(i) for i in registry.model_ids)

    def test_default_registry_count_is_paid_only(self, full_bundle, raw_models):
        paid = [m for m in raw_models if not is_free_tier(m["model_id"])]
        registry = ModelRegistry.from_bundle(full_bundle)
        assert len(registry) == len(paid) == 322

    def test_load_from_bundle_skips_free_by_default(self, full_bundle):
        registry = ModelRegistry()
        count = registry.load_from_bundle(full_bundle)
        assert count == 322
        assert not any(is_free_tier(i) for i in registry.model_ids)

    def test_load_preset_loads_the_default_catalog(self):
        registry = ModelRegistry()
        count = registry.load_preset("default")
        assert count == len(starter_bundle().model_entries())
        assert not any(is_free_tier(i) for i in registry.model_ids)


class TestFreeDataIsPreservedForOptIn:
    def test_raw_preset_still_ships_free_models(self, raw_models):
        """The data is kept on purpose -- the future dedicated free-models
        functionality reads the same catalog with include_free=True."""
        free = [m["model_id"] for m in raw_models if is_free_tier(m["model_id"])]
        assert len(free) >= 10, "expected the full catalog to keep shipping free-tier data"

    def test_include_free_opt_in_loads_everything(self, full_bundle, raw_models):
        registry = ModelRegistry.default(include_free=True, bundle=full_bundle)
        assert len(registry) == len(raw_models) == 362
        assert any(is_free_tier(i) for i in registry.model_ids)

    def test_opt_in_free_models_are_fully_usable_entries(self, full_bundle):
        """When the future feature opts in, free models must be complete
        (scores, latency) -- not degraded stubs."""
        registry = ModelRegistry.default(include_free=True, bundle=full_bundle)
        free = [m for m in registry.all_models if is_free_tier(m.model_id)]
        assert free
        for m in free:
            assert m.benchmark_scores, f"{m.model_id} has no scores"
            assert m.latency is not None
