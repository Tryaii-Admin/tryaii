# Changelog

## 0.1.0 (2026-03-29)

- Initial release as TryAii-DRE monorepo (ported from diffrential)
- Python and Node core packages with routing engine support
- 35+ models from 6 providers (OpenAI, Anthropic, Google, xAI, DeepSeek, Mistral)
- 12 standard benchmarks (MMLU, HumanEval, SWE-bench, GSM8K, MT-Bench, etc.)
- Embedding-based semantic classification with keyword fallback
- 3-factor scoring engine (quality, cost, speed) with user priorities
- OpenRouter active routing integration
- CLI tool (tryaii-dre route, models, benchmarks, setup, regenerate)
- LRU cache with TTL for embeddings and classifications
- Pre-computed centroids for zero first-run delay
