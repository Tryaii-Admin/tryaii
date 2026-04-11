"""
TryAii-DRE Quickstart -- Minimum viable example.

pip install tryaii-dre
python examples/quickstart.py
"""

from tryaii_dre import Router, Priorities

# Create router (uses local embeddings, ships with 35+ models)
router = Router()

# Route a prompt -- returns the best model with reasoning
result = router.route("Write a Python function to merge two sorted arrays")
print(f"Best model: {result.best_model}")
print(f"Reasoning: {result.best_reasoning}")
print(f"Score: {result.best_score:.3f}")
print()

# Route with custom priorities
result = router.route(
    "Explain quantum entanglement in simple terms",
    priorities=Priorities(quality=5, cost=1, speed=2),
    top_k=3,
)

print(f"Best model for quality-first: {result.best_model}")
for score in result.scores:
    print(f"  {score.model_id}: {score.final_score:.3f} -- {score.reasoning}")
print()

# Budget-focused routing
result = router.route(
    "Summarize this email thread",
    priorities=Priorities.budget(),
)
print(f"Cheapest good model: {result.best_model}")
print()

# Filter by provider
result = router.route(
    "Debug this React component",
    filter_provider="Anthropic",
)
print(f"Best Anthropic model for debugging: {result.best_model}")
