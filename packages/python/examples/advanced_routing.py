"""
Advanced Routing -- Custom registries, filtering, and multi-step agents.

Shows how to build sophisticated routing logic on top of TryAii-DRE.
"""

from tryaii import TryaiiDreConfig, ModelRegistry, Priorities, Router

# ---- Custom model registry ----

registry = ModelRegistry()  # Start empty

# Add only the models you want to route between
registry.add(
    "gpt-4o-mini",
    provider="OpenAI",
    benchmarks={"HumanEval": 87.2, "MMLU": 70.0, "GSM8K": 87.0},
    pricing=(0.00015, 0.0006),
    latency="very fast",
    capabilities=["fast-response", "cost-effective"],
)
registry.add(
    "claude-sonnet-4-5-20250929",
    provider="Anthropic",
    benchmarks={"HumanEval": 93.0, "SWE-bench": 77.2, "MMLU": 89.1},
    pricing=(0.003, 0.015),
    latency="medium",
    capabilities=["advanced-reasoning", "code-generation"],
)
registry.add(
    "gemini-2.5-flash",
    provider="Google",
    benchmarks={"HumanEval": 78.0, "MMLU": 84.0, "GSM8K": 92.0},
    pricing=(0.00015, 0.00035),
    latency="fast",
    capabilities=["fast-response", "multimodal"],
)

router = Router(registry=registry)


# ---- Multi-step agent routing ----
# Different steps of an agent pipeline need different model strengths

def route_agent_step(step_type: str, prompt: str) -> str:
    """Route each agent step to the optimal model."""
    strategies = {
        "planning": Priorities(quality=5, cost=1, speed=1),
        "code_generation": Priorities(quality=5, cost=2, speed=2),
        "summarization": Priorities(quality=2, cost=5, speed=4),
        "validation": Priorities(quality=4, cost=3, speed=3),
    }

    priorities = strategies.get(step_type, Priorities.balanced())
    result = router.route(prompt, priorities=priorities)

    print(f"  [{step_type}] -> {result.best_model} (score: {result.best_score:.3f})")
    return result.best_model


print("Agent pipeline routing:")
route_agent_step("planning", "Plan the architecture for a REST API")
route_agent_step("code_generation", "Write the authentication middleware")
route_agent_step("summarization", "Summarize the changes made in this PR")
route_agent_step("validation", "Check this code for security vulnerabilities")


# ---- Cost-constrained routing ----

print("\n\nCost-constrained routing (max $0.001/1k input):")
result = router.route(
    "Write unit tests for this function",
    filter_max_cost=0.001,
)
print(f"  Best under $0.001: {result.best_model}")
for s in result.scores:
    m = router.models.get_model(s.model_id)
    cost = f"${m.pricing.input_per_1k:.5f}" if m and m.pricing else "?"
    print(f"    {s.model_id}: score={s.final_score:.3f}, input_cost={cost}")


# ---- Custom embedding model ----

print("\n\nUsing a different embedding model:")
config = TryaiiDreConfig(embedding_model="all-mpnet-base-v2")
custom_router = Router(config=config, registry=registry)

result = custom_router.route("Optimize this SQL query for performance")
print(f"  Best model: {result.best_model}")
print(f"  Classifier: {result.classification.classifier_used}")
