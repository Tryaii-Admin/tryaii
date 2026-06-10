# TryAii-DRE

**Embedding-based AI model router.** Understands your prompt semantically and routes to the best model based on benchmarks, cost, speed, and quality.

```python
from tryaii import DREClient, Router

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
pip install tryaii
```

The base install includes local embeddings via `sentence-transformers` - no API keys needed.

Optional extras for provider integrations:

```bash
pip install tryaii[openrouter]  # Route & call models via OpenRouter (adds httpx)
pip install tryaii[openai]      # Use OpenAI embeddings instead of local (adds openai)
pip install tryaii[redis]       # Redis client for planned distributed cache (not yet implemented)
pip install tryaii[all]         # All optional integrations
```

## Quick Start

```python
from tryaii import Router, Priorities

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

## CLI

Installing the package adds a `tryaii` command (same surface as the Node SDK). It opens
with an animated blue→red banner, then runs your command. The banner prints to stderr and
auto-suppresses when output is piped, so `--json` stays clean.

```bash
tryaii route "Write a Python function to merge sorted arrays" --quality=5 --cost=1
tryaii eval prompts.json --output results/my-run --quality=5 --cost=1 --speed=1
tryaii models --provider anthropic        # add --json for machine-readable output
tryaii benchmarks --json
tryaii setup                               # download the embedding model + warm centroids
```

| Command | Key options |
|---------|-------------|
| `route "<prompt>"` | `--quality/--cost/--speed <1-5>` (default 3), `--top-k <n>` |
| `eval <input.json>` | `-o/--output <dir>`, `--max-price <usd>`, `--output-tokens <n>`, `--budget-mode strict\|fit-output` |
| `models` | `--provider <name>`, `--json` |
| `benchmarks` | `--json` |
| `setup` / `regenerate` | `--model <name>` |

Global flags: `--no-banner` (or `TRYAII_NO_BANNER=1`), `NO_COLOR=1`, `-v/--verbose`,
`-V/--version`. All flags work in any position and match the npm CLI.

### Eval over a dataset

```bash
# Balanced run into a named folder
tryaii eval prompts.json --output results/my-run --quality=5 --cost=1 --speed=1

# Budget-aware: --max-price is the total budget for the whole dataset
tryaii eval prompts.json --output results/budget --max-price=0.10 --output-tokens=2000
tryaii eval prompts.json --output results/budget-fit --max-price=0.10 --output-tokens=2000 --budget-mode=fit-output
```

The input can be an array of strings or objects with `prompt`, optional `id`,
and optional `category`. In budgeted eval, quality/cost/speed priority flags
are ignored: price is the hard constraint, and the optimizer maximizes model
quality within that price. `--budget-mode=fit-output` lowers the fixed output
token estimate when the requested length cannot fit the total budget. The
command writes `results.jsonl`, `summary.json`, and `index.html`.

## OpenRouter Integration

```python
from tryaii import Router
from tryaii.integrations import OpenRouterIntegration

router = Router()
openrouter = OpenRouterIntegration(router, api_key="sk-or-...")

response = openrouter.chat("Write a quicksort implementation")
print(response.model_used)  # Auto-selected best model
print(response.content)     # Actual response
```

## OpenAI Embeddings

```python
from tryaii import Router
from tryaii.embeddings import OpenAIEmbeddingProvider

router = Router(
    embedding_provider=OpenAIEmbeddingProvider(),
)

result = router.route("Summarize this architecture decision")
print(result.best_model)
```

Install the OpenAI client first:

```bash
pip install tryaii[openai]
```

## License

Apache 2.0
