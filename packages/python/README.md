# TryAii-DRE

**Embedding-based AI model router.** Understands your prompt semantically and routes to the best model based on benchmarks, cost, speed, and quality.

```python
from tryaii_dre import DREClient, Router

router = Router()
result = router.route("Write a Python function to merge sorted arrays")

print(result.best_model)     # "gpt-5.2"
print(result.best_reasoning) # "Quality: 0.94 on [HumanEval (93%), SWE-bench (87%)]"

client = DREClient(api_key="sk-or-...")
response = client.chat("Write a quicksort implementation")
print(response.content)
```

## Install

```bash
pip install tryaii-dre
```

The base install includes local embeddings via `sentence-transformers` - no API keys needed.

Optional extras for provider integrations:

```bash
pip install tryaii-dre[openrouter]  # Route & call models via OpenRouter (adds httpx)
pip install tryaii-dre[openai]      # Use OpenAI embeddings instead of local (adds openai)
pip install tryaii-dre[redis]       # Redis client for planned distributed cache (not yet implemented)
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

## Eval CLI

Run routing over a JSON dataset:

```bash
tryaii-dre eval prompts.json --output results/my-run --quality=5 --cost=1 --speed=1
```

Budget-aware eval:

```bash
tryaii-dre eval prompts.json --output results/budget --max-price=0.10 --output-tokens=2000
tryaii-dre eval prompts.json --output results/budget-fit --max-price=0.10 --output-tokens=2000 --budget-mode=fit-output
```

The input can be an array of strings or objects with `prompt`, optional `id`,
and optional `category`. In budgeted eval, quality/cost/speed priority flags
are ignored: price is the hard constraint, and the optimizer maximizes model
quality within that price. `--budget-mode=fit-output` lowers the fixed output
token estimate when the requested length cannot fit the total budget. The
command writes `results.jsonl`, `summary.json`, and `index.html`.

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

## OpenAI Embeddings

```python
from tryaii_dre import Router
from tryaii_dre.embeddings import OpenAIEmbeddingProvider

router = Router(
    embedding_provider=OpenAIEmbeddingProvider(),
)

result = router.route("Summarize this architecture decision")
print(result.best_model)
```

Install the OpenAI client first:

```bash
pip install tryaii-dre[openai]
```

## License

Apache 2.0
