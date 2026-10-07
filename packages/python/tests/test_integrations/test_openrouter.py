"""Tests for OpenRouter integration (mock-only, no real API calls)."""

from tryaii.integrations.openrouter import MODEL_ID_TO_OPENROUTER, OpenRouterIntegration


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

    def test_all_default_models_resolve_to_openrouter_slugs(self):
        """Every default-catalog id must resolve to an OpenRouter-usable slug.

        The remote catalog ships OpenRouter-native `provider/model` ids, so the
        resolver's identity fallback covers them; legacy bare ids (gpt-4o, ...)
        still go through MODEL_ID_TO_OPENROUTER. Either way the resolved value
        must be a provider-prefixed slug.
        """
        from tryaii.registry.models import ModelRegistry

        registry = ModelRegistry.default()
        unresolvable = []
        for model_id in registry.model_ids:
            slug = MODEL_ID_TO_OPENROUTER.get(model_id, model_id)
            if "/" not in slug:
                unresolvable.append(model_id)

        assert not unresolvable, f"Models with no OpenRouter slug: {unresolvable}"

    def test_resolve_model_passthrough_for_native_ids(self):
        """`_resolve_model` must return OpenRouter-native ids unchanged."""
        resolve = OpenRouterIntegration._resolve_model
        assert resolve(None, "openai/gpt-5") == "openai/gpt-5"
        assert resolve(None, "qwen/qwen3.7-max") == "qwen/qwen3.7-max"

    def test_resolve_model_maps_legacy_ids(self):
        """Legacy bare ids still translate through MODEL_ID_TO_OPENROUTER."""
        resolve = OpenRouterIntegration._resolve_model
        assert resolve(None, "gpt-4o") == "openai/gpt-4o"
