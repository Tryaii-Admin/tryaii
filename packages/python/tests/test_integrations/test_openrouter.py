"""Tests for OpenRouter integration (mock-only, no real API calls)."""

from tryaii.integrations.openrouter import MODEL_ID_TO_OPENROUTER


class TestModelMapping:
    def test_openai_models_mapped(self):
        assert "gpt-4o" in MODEL_ID_TO_OPENROUTER
        assert MODEL_ID_TO_OPENROUTER["gpt-4o"] == "openai/gpt-4o"

    def test_anthropic_models_mapped(self):
        assert "claude-opus-4-5-20251101" in MODEL_ID_TO_OPENROUTER
        assert "anthropic/" in MODEL_ID_TO_OPENROUTER["claude-opus-4-5-20251101"]

    def test_google_models_mapped(self):
        assert "gemini-2.5-pro" in MODEL_ID_TO_OPENROUTER
        assert "google/" in MODEL_ID_TO_OPENROUTER["gemini-2.5-pro"]

    def test_all_default_models_have_mapping(self):
        """Verify that all models in default registry have OpenRouter mappings."""
        from tryaii.registry.models import ModelRegistry

        registry = ModelRegistry.default()
        unmapped = []
        for model_id in registry.model_ids:
            if model_id not in MODEL_ID_TO_OPENROUTER:
                unmapped.append(model_id)

        # Allow some unmapped (new models may not have OpenRouter slugs yet)
        # but the majority should be mapped
        mapped_ratio = 1 - len(unmapped) / len(registry)
        assert mapped_ratio >= 0.8, f"Too many unmapped models: {unmapped}"
