# TryAii-DRE

**TryAii Diff Routing Engine** -- Embedding-based AI model router that understands your prompt semantically and routes to the best LLM based on benchmarks, cost, speed, and quality.

## Packages

| Package | Install | Description |
|---------|---------|-------------|
| [Python core](packages/python/) | `pip install tryaii-dre` | Routing engine for Python |
| [Node core](packages/node/) | `npm install tryaii-dre` | TypeScript routing engine |
| [Python SDK](packages/sdk/python/) | `pip install tryaii-dre-sdk` | High-level client + middleware |
| [Node SDK](packages/sdk/node/) | `npm install tryaii-dre-sdk` | Express/Next.js middleware |

## Quick Start (Python)

```bash
pip install tryaii-dre
```

```python
from tryaii_dre import Router, Priorities

router = Router()
result = router.route("Write a Python function to merge sorted arrays")

print(result.best_model)      # "grok-4-fast"
print(result.best_reasoning)  # "Quality: 0.91 on [HumanEval (95%), ...]"
```

## How It Works

```
User Prompt
    |
    v
[Embed locally]  -->  Cosine similarity vs 12 benchmark centroids
    |                  (HumanEval, MMLU, GSM8K, SWE-bench, ...)
    v
[Classify task]  -->  "This is a CODE_TECHNICAL task"
    |
    v
[Score models]   -->  quality * weight + cost * weight + speed * weight
    |
    v
Top-K Ranked Models with reasoning
```

## Architecture

```
tryaii-dre/
  shared/                  Single source of truth for model data
    models/                35+ models with benchmarks and pricing
    benchmarks/            12 standard benchmark definitions
    centroids/             Pre-computed embedding centroids
  packages/
    python/                pip install tryaii-dre
    node/                  npm install tryaii-dre
    sdk/                   Higher-level wrappers
  scripts/                 Build and sync tooling
```

## Models Included

35+ models from 6 providers, pre-loaded with benchmark scores and pricing:

- **OpenAI**: GPT-5.2, GPT-5.1, GPT-5, O3, O4-mini, GPT-4o, GPT-4.1, and more
- **Anthropic**: Claude Opus 4.5, Claude Sonnet 4.5, Claude Sonnet 4, Claude Haiku 4.5
- **Google**: Gemini 3 Pro, Gemini 3 Flash, Gemini 2.5 Pro/Flash
- **xAI**: Grok 4, Grok 4 Fast, Grok Code Fast, Grok 3
- **DeepSeek**: Reasoner, Chat
- **Mistral**: Large, Small

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

## License

Apache 2.0 — see [LICENSE](LICENSE).
