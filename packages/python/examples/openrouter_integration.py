"""
OpenRouter Integration -- Active routing that actually calls the model.

Combines TryAii's semantic routing with OpenRouter's multi-provider API.
The router picks the best model, then OpenRouter handles the API call.

pip install tryaii[openrouter]
export OPENROUTER_API_KEY=sk-or-...
"""

from tryaii import Router
from tryaii.integrations import OpenRouterIntegration

# Create router + OpenRouter integration
router = Router()
openrouter = OpenRouterIntegration(router)

# --- Auto-routed chat ---
# TryAii picks the best model, OpenRouter calls it
response = openrouter.chat(
    "Write a Python function to find the longest palindromic substring",
    priorities={"quality": 5, "cost": 2, "speed": 3},
)

print(f"Model used: {response.model_used}")
print(f"OpenRouter slug: {response.openrouter_model}")
print(f"Why: {response.route_reasoning}")
print(f"Response:\n{response.content}")
print(f"Tokens: {response.usage}")

# --- Streaming ---
print("\n--- Streaming response ---")
for chunk in openrouter.stream(
    "Explain the difference between TCP and UDP",
    priorities={"quality": 3, "cost": 4, "speed": 5},
):
    print(chunk, end="", flush=True)
print()

# --- Override model (skip routing) ---
response = openrouter.chat(
    "Hello!",
    override_model="gpt-4o-mini",  # Force a specific model
)
print(f"\nOverride model: {response.model_used}")
print(f"Response: {response.content}")
