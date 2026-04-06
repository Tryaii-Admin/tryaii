# Centroids & Data Update Strategy

## How It Works Today

The router classifies prompts by comparing their embedding vector against **benchmark centroids** — precomputed average vectors for each benchmark category (HumanEval, MMLU, etc.).

### Current centroid loading priority (see `tryaii_dre/centroids/loader.py`):

1. **In-memory cache** — already loaded this session
2. **User cache** (`~/.tryaii_dre/centroids/`) — previously generated for their embedding model
3. **Bundled static file** (`tryaii_dre/centroids/data/centroids_all-MiniLM-L6-v2.json`) — ships with the package, zero delay
4. **Generate fresh** — only if using a non-default embedding model; computed from `training_queries.json` and cached to disk

This means `pip install tryaii-dre` works fully offline with no first-run computation for the default model.

## What Needs to Change: Remote Update System

When new models are published or benchmark data changes on our server, the package should be able to fetch updated data without requiring a new pip release.

### Data that can be updated remotely

| Data | Location today | Update frequency |
|------|---------------|-----------------|
| Model registry (scores, pricing, latency) | `tryaii_dre/registry/data/` | When new models launch or prices change |
| Benchmark centroids | `tryaii_dre/centroids/data/` | When training queries are revised |
| Training queries | `tryaii_dre/centroids/data/training_queries.json` | Rarely |

### Proposed architecture

```
Package (offline default)          Remote API
┌─────────────────────┐           ┌──────────────────┐
│ Bundled centroids    │           │ GET /v1/manifest  │ ← version check
│ Bundled model scores │  ──────► │ GET /v1/models    │ ← updated registry
│ Bundled queries      │           │ GET /v1/centroids │ ← updated centroids
└─────────────────────┘           └──────────────────┘
         ▲                                 │
         │         ~/.tryaii_dre/cache/     │
         └──────── cached remote data ◄────┘
```

### Design principles

1. **Offline by default** — `Router()` never makes network calls unless the user opts in. Packages that phone home silently are a trust problem.

2. **Explicit update** — two ways to trigger:
   - **CLI**: `tryaii-dre update` — fetches latest data from the API and caches locally
   - **Python**: `router.update()` — same thing, programmatic

3. **Optional auto-check** — `Router(auto_update=True)` or env var `TRYAII_DRE_AUTO_UPDATE=1`. Non-blocking check on init; if fresh data is available, download and cache it. Print a notice, don't fail silently.

4. **Version-based cache invalidation** — the API exposes a `/v1/manifest` with a version or timestamp. The client stores the last-fetched version. On update check, compare versions; skip download if current.

5. **Graceful fallback** — if the API is unreachable, use cached data. If no cache, use bundled data. Never crash because of a network issue.

### Suggested config additions (`TryaiiDreConfig`)

```python
# Remote update settings
api_base_url: str = "https://api.tryaii.com"
auto_update: bool = False          # opt-in
update_cache_ttl: int = 86400      # seconds (24h) before re-checking
```

### Suggested API endpoints

```
GET /v1/manifest
  → { "version": "2026-04-06", "models_hash": "abc123", "centroids_hash": "def456" }

GET /v1/models
  → Updated model registry JSON (same schema as bundled data)

GET /v1/centroids?model=all-MiniLM-L6-v2
  → Updated centroids JSON for the specified embedding model
```

### Implementation order

1. **Ship v0.1.0 with bundled-only data** (current state after packaging fixes)
2. Add `api_base_url` and `auto_update` to `TryaiiDreConfig`
3. Implement `/v1/manifest` endpoint on the server
4. Add `router.update()` method that fetches manifest → compares → downloads if stale
5. Add `tryaii-dre update` CLI command
6. Add `auto_update` logic to `Router.__init__` (non-blocking, behind opt-in flag)
7. Add `/v1/models` and `/v1/centroids` server endpoints

### Cache layout

```
~/.tryaii_dre/
├── centroids/
│   └── centroids_all-MiniLM-L6-v2.json   ← user-generated or fetched
├── cache/
│   ├── manifest.json                       ← last fetched manifest
│   ├── models.json                         ← last fetched model registry
│   └── centroids_all-MiniLM-L6-v2.json   ← last fetched centroids
└── config.json                             ← optional local config overrides
```

### Security notes

- All API calls over HTTPS only
- Consider signing manifests so the client can verify data integrity
- Never send user prompts or usage data to the update API
- The update endpoint is read-only; no auth required for public model data
