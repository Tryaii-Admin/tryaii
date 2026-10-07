# TryAii

**Embedding-based AI model router.** Understands your prompt semantically and routes to the best model based on benchmarks, cost, speed, and quality.

```python
from tryaii import DREClient, Router

router = Router()
result = router.route("Write a Python function to merge sorted arrays")

print(result.best_model)     # e.g. "anthropic/claude-opus-5.5"
print(result.best_reasoning) # "q'=0.98 (4 real of 5) | imputed: 1/5 | cost 0.2066 ($12.00/M) | ..."

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

## Model catalog

The package ships a **starter catalog**: 45 well-known models from OpenAI, Anthropic,
Google, DeepSeek, xAI and Mistral, scored on 16 benchmarks. It works offline with no
account. Log in (free, with your tryaii.com Google account) to route on the **full
catalog of 322 models**:

```bash
tryaii login     # device sign-in: open the printed URL in any browser and approve the code
tryaii whoami    # the signed-in account and the downloaded catalog release
tryaii logout    # remove the credentials and the downloaded catalog
```

After login the full catalog downloads automatically, is refreshed at most once a day,
cached under `~/.tryaii/catalog/` (or `TRYAII_DRE_DATA_DIR`), and used only after its
Ed25519 signature checks out. In code:

```python
Router()                    # catalog="auto": full when logged in, else starter
Router(catalog="starter")   # always the packaged catalog, no network
Router(catalog="full")      # raises LoginRequiredError when not logged in
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
tryaii cachelint request.json              # pre-flight prompt-cache analysis (warn-only)
tryaii login                               # free: unlock the full model catalog
```

| Command | Key options |
|---------|-------------|
| `route "<prompt>"` | `--quality/--cost/--speed <1-5>` (default 3), `--top-k <n>` |
| `eval <input.json>` | `-o/--output <dir>`, `--max-price <usd>`, `--output-tokens <n>`, `--budget-mode strict\|fit-output` |
| `cachelint <input.json \| ->` | `--json`, raw-text mode via `--provider <name>` + `--model <name>`; exact OpenAI/xAI counts need `pip install tryaii[cachelint]` |

The cachelint engine also runs inside the clients: pass `cache_lint="warn"` to
`DREClient`/`AsyncDREClient`/`OpenRouterIntegration` (or set
`TRYAII_CACHE_LINT=warn`) to lint every outgoing chat request and verify
cache predictions against the response `usage` — warn-only and fail-open.

| `models` | `--provider <name>`, `--json` |
| `benchmarks` | `--json` |
| `setup` / `regenerate` | `--model <name>` |
| `login` / `logout` | none |
| `whoami` | `--json` |

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
