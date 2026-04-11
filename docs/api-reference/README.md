# TryAii-Bench API

A read-only REST API serving LLM benchmark data (models, pricing, capabilities, speed, and scores) for ~335 OpenRouter models, backed by Supabase/Postgres and deployed via `render.yaml` (`api/Dockerfile`, Oregon region, starter plan, auto-deploy on push to `main`).

**Base URL:** https://tryaii-bench-api.onrender.com

- **Swagger UI** — https://tryaii-bench-api.onrender.com/docs
- **OpenAPI spec** — https://tryaii-bench-api.onrender.com/openapi.json
- **Public overview** — https://tryaii-bench-api.onrender.com/

---

## Authentication

All routes except `GET /` require an API key via header:

```
x-api-key: <your-key>
```

If the server's `API_KEY` env var is empty, auth is disabled (local dev only). On Render it is set — you must pass the header.

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Public overview + endpoint catalogue (no auth) |
| `GET` | `/summary` | Row counts and unique-model counts per dataset |
| `GET` | `/models` | All model metadata (name, context length, modality…) |
| `GET` | `/models/list` | Lightweight `{model_id, name}` list for autocomplete |
| `GET` | `/models/{model_id}` | One model's metadata |
| `GET` | `/models/{model_id}/full` | Combined view: model + pricing + capabilities + benchmarks + speed |
| `GET` | `/pricing` | All pricing rows (`prompt_cost_per_1m_tokens`, …) |
| `GET` | `/pricing/{model_id}` | Pricing for one model |
| `GET` | `/capabilities` | All capability flag rows |
| `GET` | `/capabilities/{model_id}` | Capabilities for one model |
| `GET` | `/benchmarks/static` | Published scores (HF v2, PwC, SWE-bench, HELM, AA, vendor cards) |
| `GET` | `/benchmarks/static/{model_id}` | Static scores for one model |
| `GET` | `/benchmarks/live` | Live scores (Chatbot Arena, MT-Bench, LiveBench) |
| `GET` | `/benchmarks/live/{model_id}` | Live scores for one model |
| `GET` | `/speed` | Per-provider speed (`tokens_per_second`, `time_to_first_token_ms`, …) |
| `GET` | `/speed/{model_id}` | Speed across all providers for one model |
| `GET` | `/admin/refresh` | Clears the 5-minute in-memory cache (does not run the pipeline) |

`{model_id}` carries the provider prefix (e.g. `openai/gpt-4o`, `anthropic/claude-opus-4.6`). All single-model routes use FastAPI's `:path` converter, so the `/` inside the ID is preserved.

### What the benchmark endpoints contain

- **`/benchmarks/static`** — HF Open LLM Leaderboard v2 (IFEval, BBH, MATH, GPQA, MUSR, MMLU-PRO), PapersWithCode HF Archive (HumanEval, MMLU, GSM8K, DROP), SWE-bench (Verified, Lite), Salt curated comparison, OpenAI simple-evals README, DeepSeek / Meta Llama / Microsoft Phi vendor cards, Stanford HELM Lite, Artificial Analysis.
- **`/benchmarks/live`** — Chatbot Arena Elo (text, code, vision, document, search), MT-Bench, LiveBench (6 contamination-free categories).

All scores are on a 0–100 percentage scale. No cross-benchmark normalization.

---

## Query parameters (list endpoints)

`/models`, `/pricing`, `/capabilities`, `/benchmarks/static`, `/benchmarks/live`, `/speed` accept:

| Param | Type | Description | Example |
|-------|------|-------------|---------|
| `model_id` | string | Exact match filter | `?model_id=openai/gpt-4o` |
| `benchmark` | string | Benchmark name (case-insensitive) | `?benchmark=IFEval` |
| `provider` | string | Provider filter (speed only) | `?provider=Together` |
| `min_score` | float | Minimum score | `?min_score=0.8` |
| `max_score` | float | Maximum score | `?max_score=0.95` |
| `sort` | string | Field name to sort by | `?sort=score` |
| `order` | string | `asc` (default) or `desc` | `?order=desc` |
| `limit` | int | 1–10000 | `?limit=20` |
| `offset` | int | Pagination skip | `?offset=20` |

`total_results` in every response is the match count before pagination so clients can page correctly.

---

## Usage examples

### curl

```bash
KEY="your-api-key"
BASE="https://tryaii-bench-api.onrender.com"

# Public overview (no auth)
curl "$BASE/"

# Data summary
curl -H "x-api-key: $KEY" "$BASE/summary"

# All model IDs and display names
curl -H "x-api-key: $KEY" "$BASE/models/list"

# Full profile for one model (model + pricing + caps + benchmarks + speed)
curl -H "x-api-key: $KEY" "$BASE/models/openai/gpt-4o/full"

# Top 10 IFEval scores
curl -H "x-api-key: $KEY" \
  "$BASE/benchmarks/static?benchmark=IFEval&sort=score&order=desc&limit=10"

# Cheapest 10 models
curl -H "x-api-key: $KEY" \
  "$BASE/pricing?sort=prompt_cost_per_1m_tokens&order=asc&limit=10"

# Fastest models on Together
curl -H "x-api-key: $KEY" \
  "$BASE/speed?provider=Together&sort=tokens_per_second&order=desc"

# Force a fresh read from Postgres (clear cache)
curl -H "x-api-key: $KEY" "$BASE/admin/refresh"
```

### Python

```python
import os, requests

BASE_URL = "https://tryaii-bench-api.onrender.com"
HEADERS = {"x-api-key": os.environ["TRYAII_API_KEY"]}

# List all models
r = requests.get(f"{BASE_URL}/models/list", headers=HEADERS)
for m in r.json()["data"][:5]:
    print(f"{m['model_id']:40s} {m['name']}")

# Full profile for Claude Opus
r = requests.get(f"{BASE_URL}/models/anthropic/claude-opus-4.6/full", headers=HEADERS)
profile = r.json()["data"]
print(f"Prompt cost: ${profile['pricing']['prompt_cost_per_1m_tokens']}/1M tokens")
print(f"{len(profile['benchmarks_static'])} static benchmark scores")

# Top IFEval scores
r = requests.get(
    f"{BASE_URL}/benchmarks/static",
    headers=HEADERS,
    params={"benchmark": "IFEval", "sort": "score", "order": "desc", "limit": 10},
)
for row in r.json()["data"]:
    print(f"{row['model_id']:40s} {row['score']:.2f}")
```

### JavaScript

```javascript
const BASE_URL = "https://tryaii-bench-api.onrender.com";
const HEADERS  = { "x-api-key": process.env.TRYAII_API_KEY };

// Public overview
const about = await fetch(`${BASE_URL}/`).then(r => r.json());
console.log(about.name, about.version);

// Top 10 IFEval
const bench = await fetch(
  `${BASE_URL}/benchmarks/static?benchmark=IFEval&sort=score&order=desc&limit=10`,
  { headers: HEADERS },
).then(r => r.json());
bench.data.forEach(b => console.log(b.model_id, b.score));
```

---

## Response shape

Every list endpoint returns:

```json
{
  "data": [ ... ],
  "total_results": 123,
  "limit": 100,
  "offset": 0
}
```

Single-model endpoints return `{ "data": { ... } }`. `/models/{id}/full` parallelizes 5 sub-queries via `asyncio.gather` and returns `model + pricing + capabilities + benchmarks_static + benchmarks_live + speed` in one payload.

---

## Render deployment notes

Config lives in `render.yaml` at the API repo root:

- `type: web`, `runtime: docker`, `dockerfilePath: api/Dockerfile`, `dockerContext: .`
- Region `oregon`, plan `starter`, auto-deploy on push to `main`
- Health check: `GET /`
- Two secret env vars (set in Render dashboard, `sync: false`):
  - `DATABASE_URL` — Supabase Postgres connection string
  - `API_KEY` — the key clients must send in `x-api-key`

Behaviour to know:

- **Stateless** — all data lives in Supabase; redeploys lose nothing.
- **Starter plan** stays warm (free tier would cold-start ~30s after idle).
- **Cache** — each dataset is held in memory for 5 minutes after first read; `/admin/refresh` clears it early. It does not run the data pipeline — the pipeline runs on its own GitHub Actions schedule every 6h and writes to Supabase directly.

The full in-repo docs live at `api/README.md` in the benchmarks API repo, and `build_excel.py` already wires the deployed URL as its default base (`TRYAII_API_BASE` defaults to `https://tryaii-bench-api.onrender.com`).
