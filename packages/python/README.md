# TryAii-DRE

**Embedding-based AI model router.** Understands your prompt semantically and routes to the best model based on benchmarks, cost, speed, and quality.

```python
from tryaii_dre import Router

router = Router()
result = router.route("Write a Python function to merge sorted arrays")

print(result.best_model)     # "gpt-5.2"
print(result.best_reasoning) # "Quality: 0.94 on [HumanEval (93%), SWE-bench (87%)]"
```

## Install

```bash
pip install tryaii-dre
```

The base install includes local embeddings via `sentence-transformers` — no API keys needed.

Optional extras for provider integrations:
```bash
pip install tryaii-dre[openrouter]  # Route & call models via OpenRouter (adds httpx)
pip install tryaii-dre[openai]      # Use OpenAI embeddings instead of local (adds openai)
pip install tryaii-dre[redis]       # Redis-backed embedding cache (adds redis)
pip install tryaii-dre[all]         # All optional integrations
```

## Quick Start

```python
from tryaii_dre import Router, Priorities

router = Router()

# Route with default balanced priorities
result = router.route("Explain quantum entanglement simply")
print(result.best_model)

# Quality-first (ignore cost)
result = router.route(
    "Debug this memory leak in my Node.js app",
    priorities=Priorities(quality=5, cost=1, speed=2),
)

# Budget mode
result = router.route(
    "Summarize this email",
    priorities=Priorities.budget(),
)
```

## OpenRouter Integration

```python
from tryaii_dre import Router
from tryaii_dre.integrations import OpenRouterIntegration

router = Router()
openrouter = OpenRouterIntegration(router, api_key="sk-or-...")

response = openrouter.chat("Write a quicksort implementation")
print(response.model_used)  # Auto-selected best model
print(response.content)     # Actual response
```

## License

Apache 2.0
