"""
TryAii -- Try It Out

Shows the full flow: routing decisions, priority presets,
filtering, and live OpenRouter calls.

Setup:
    pip install tryaii[openrouter]
    set OPENROUTER_API_KEY=sk-or-...
"""

from tryaii import Router, Priorities
from tryaii.integrations import OpenRouterIntegration

# -- 1. See how routing works (no API key needed) --------------------

router = Router()

prompts = [
    "Write a Python function to merge two sorted arrays",
    "Explain quantum entanglement to a 10 year old",
    "Calculate the derivative of x^3 * sin(x)",
    "Draft a professional email declining a meeting",
    "Find the bug in this React useEffect hook",
]

print("=" * 70)
print("ROUTING DECISIONS (local only, no API calls)")
print("=" * 70)

for prompt in prompts:
    result = router.route(prompt, top_k=3)
    print(f"\nPrompt: {prompt}")
    print(f"  Task type : {result.classification.broad_category} > {result.classification.subcategory}")
    print(f"  Confidence: {result.classification.confidence:.3f}")
    for i, s in enumerate(result.scores, 1):
        print(f"  #{i} {s.model_id:30s} score={s.final_score:.3f}  ({s.reasoning})")


# -- 2. Priority presets change the ranking -------------------------

print("\n" + "=" * 70)
print("SAME PROMPT, DIFFERENT PRIORITIES")
print("=" * 70)

prompt = "Write unit tests for a payment processing module"

for name, prio in [
    ("Balanced",     Priorities.balanced()),
    ("Performance",  Priorities.performance()),
    ("Budget",       Priorities.budget()),
    ("Fast",         Priorities.fast()),
    ("Custom(q=5,c=4,s=1)", Priorities(quality=5, cost=4, speed=1)),
]:
    result = router.route(prompt, priorities=prio, top_k=1)
    s = result.scores[0]
    print(f"  {name:25s} -> {s.model_id:30s} score={s.final_score:.3f}")


# -- 3. Filter by provider -----------------------------------------

print("\n" + "=" * 70)
print("FILTER BY PROVIDER")
print("=" * 70)

for provider in ["Anthropic", "OpenAI", "Google"]:
    result = router.route(prompt, filter_provider=provider, top_k=1)
    if result.scores:
        s = result.scores[0]
        print(f"  Best {provider:10s} -> {s.model_id:30s} score={s.final_score:.3f}")


# -- 4. Call OpenRouter for real ------------------------------------

print("\n" + "=" * 70)
print("LIVE OPENROUTER CALL")
print("=" * 70)

openrouter = OpenRouterIntegration(router)  # reads OPENROUTER_API_KEY from env

# Auto-routed: tryaii picks the model, OpenRouter calls it
response = openrouter.chat(
    "Write a Python one-liner that flattens a nested list",
    priorities={"quality": 4, "cost": 3, "speed": 4},
    max_tokens=200,
)

print(f"  Model chosen : {response.model_used}")
print(f"  OpenRouter ID: {response.openrouter_model}")
print(f"  Why          : {response.route_reasoning}")
print(f"  Tokens       : {response.usage}")
print(f"\n{response.content}")


# -- 5. Streaming --------------------------------------------------

print("\n" + "=" * 70)
print("STREAMING RESPONSE")
print("=" * 70)

print("  Model auto-selected, streaming: ", end="", flush=True)
for chunk in openrouter.stream(
    "Explain recursion in one paragraph",
    priorities={"quality": 3, "cost": 5, "speed": 4},
    max_tokens=150,
):
    print(chunk, end="", flush=True)
print("\n")
