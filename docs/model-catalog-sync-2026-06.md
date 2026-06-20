# Model catalog resync — June 2026

## Context

The DRE ships a hand-curated model catalog (`shared/models/default_models.json`,
synced into both SDKs) that mirrors the model lineup of the **tryai web app**
(`C:\Users\koren\Desktop\tryaii_Projects\tryai`). The web app refreshed its
catalog on 2026-06-13 ("add 2026 model lineup, dismiss unsupported, fix provider
params") — adding 14 frontier models, retiring 8, and **re-pricing several
existing models**. The DRE preset still declared `"updated": "2026-01"` and had
drifted out of sync.

This change does a **full resync**: the DRE catalog now matches the tryai active
lineup for membership, pricing, latency, and benchmark scores. It is a data
update only — no scoring/routing algorithm changed.

## Source of truth & data flow

The DRE catalog is **not** sourced from the tryaii-bench API (that is the
separate, future effort described in `MIGRATION.md`). It is a hand-maintained
mirror of the web app, assembled from two tryai files joined on `modelId`:

| Field in DRE preset | tryai source |
|---|---|
| `benchmark_scores` (12 benchmarks), `capabilities`, `provider` | `src/lib/services/embedding/modelsMapping.ts` → `STATIC_MODEL_BENCHMARKS` |
| `pricing.{input,output}_per_1k`, `latency`, active/retired status | `src/lib/api/services/aiModelService.ts` → `INITIAL_MODELS` |
| `description` | DRE-owned editorial (short `"Name: tagline"` form) |

Edit point and propagation:

```
shared/models/default_models.json          <-- edit here (single source of truth)
  └─ python scripts/sync-shared.py          <-- copies verbatim into both SDKs
       ├─ packages/python/tryaii/registry/presets/default_models.json
       └─ packages/node/src/registry/presets/defaultModels.json
```

Byte-for-byte equality of the two presets is enforced by
`packages/python/tests/test_parity.py::test_preset_model_data_identical_across_sdks`.

## What changed

Catalog went **33 → 39 active models** (`"updated": "2026-06"`, data `version: 0.2.0`).

**Added (14):** `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.4-nano`,
`claude-opus-4-8`, `claude-sonnet-4-6`, `claude-fable-5`, `gemini-3.5-flash`,
`gemini-3.1-pro-preview`, `gemini-3.1-flash-lite`, `deepseek-v4-pro`,
`deepseek-v4-flash`, `grok-4.3`, `mistral-medium-2508`.

**Removed (8):** the 5 models tryai marked `isActive: false`
(`o1`, `claude-3-7-sonnet-20250219`, `gemini-2.0-flash`, `gemini-3-pro-preview`,
`grok-3-mini-latest`) plus 3 dropped entirely from the active web catalog
(`grok-4-fast`, `gpt-4.1-nano`, `grok-3-latest`).

**Re-priced / re-latency'd (existing models):** pricing and `latency` were
refreshed from `INITIAL_MODELS`. Notable: `claude-opus-4-5` output
$0.075→$0.25/1k; `o3` $0.01/$0.04→$0.002/$0.008 (latency medium→slow);
`gemini-2.5-pro` $0.00075/$0.003→$0.0025/$0.015 (latency medium→slow);
`gpt-5.2` $0.00125/$0.01→$0.00175/$0.014; `grok-4-1-fast-reasoning`
$0.0005/$0.0025→$0.0002/$0.0005; several latency-tier moves
(`gpt-4o`, `gpt-5`, `gpt-4.1`, `o4-mini`, `claude-haiku-4-5`, `deepseek-reasoner`).

**OpenRouter slug map** (`integrations/openrouter.ts` / `.py`, kept in parity):
added slugs for all 14 new models. Stale entries for removed models were left in
place (harmless; avoids breaking callers still passing old IDs).

## Decisions

1. **`ARC` benchmark is DRE-owned, not imported.** For every retained model,
   tryai's `modelsMapping` matches the DRE preset on all 12 benchmarks **except
   `ARC`**: tryai's ARC column drifted to an inconsistent ARC-AGI-ish scale
   (e.g. `gpt-4o` ARC 4.5, `claude-opus-4-5` ARC 37.6) while DRE uses
   ARC-Challenge (~72–94, normalization range `(0, 95)`). Bulk-importing tryai's
   ARC would have regressed cost/quality routing for the whole catalog. So:
   - Retained models keep their existing DRE ARC values.
   - New models get a **peer-consistent ARC estimate** in DRE's scale (e.g.
     `claude-opus-4-8`≈94, `gpt-5.5`≈93, `gemini-3.1-flash-lite`≈80). These are
     editorial estimates, consistent with how tryai itself notes newer-model
     benchmarks are "partly estimated from peers." They are the only
     non-sourced numbers in this change and are flagged here for review.

2. **Arena normalization ceiling unchanged.** The highest new Arena Elo is 1510
   (`claude-fable-5`), still under the existing `(1000, 1550)` range, so no
   normalization-range edits were needed.

3. **`claude-fable-5` is access-gated in the web app** but is included in the
   DRE routable catalog (DRE recommends models; access enforcement is the
   caller's concern).

## ⚠️ Upstream data anomalies (mirrored faithfully, flag for tryai owner)

Two existing-model prices in `INITIAL_MODELS` look like upstream typos (input ≥
output, which is backwards for every other model). They were mirrored as-is to
keep DRE a faithful mirror; **verify and fix upstream**, then resync:

- `gemini-2.5-flash`: input $0.0003 / **output $0.00025** (output likely meant $0.0025).
- `gemini-3-flash-preview`: **input $0.003** / output $0.0005 (input likely meant $0.0003).

The new OpenRouter slugs for the 14 added models are best-effort (derived from
existing naming patterns) and should be verified against OpenRouter's live
catalog.

## How to redo this next time (one-time manual edit workflow)

1. Pull the active set from `INITIAL_MODELS` (`isActive: true`) and join each to
   its `STATIC_MODEL_BENCHMARKS` row on `modelId`.
2. Map fields per the table above; reconcile `ARC` per decision (1).
3. Rewrite `shared/models/default_models.json` (keep key order:
   `model_id, provider, benchmark_scores, capabilities, pricing, latency, description`;
   benchmark order: MMLU, HellaSwag, HumanEval, SWE-bench, TruthfulQA, ARC, GSM8K,
   DROP, SuperGLUE, Chatbot Arena (LMSys), MT-Bench, LiveBench; omit null LiveBench).
4. `python scripts/sync-shared.py`.
5. Add OpenRouter slugs for any new models in both `openrouter.py` and `openrouter.ts`.
6. Run verification (below).

## Verification

```bash
# Python (102 tests; includes cross-SDK parity guard + router/registry/scoring)
cd packages/python && python -m pytest -q

# Node (43 tests)
cd packages/node && npx vitest run && npx tsc --noEmit
```

Both suites pass on this change. Key guards exercised: `test_parity.py`
(node/python presets identical, normalization ranges consistent),
`test_openrouter` (≥80% of default models have OpenRouter slugs),
`router`/`scoring` (catalog loads and ranks). The Node
`router.test.ts > responds to different priorities` assertion was changed from a
brittle single-winner check to a full-ranked-order comparison (the engine pins
the top score to a constant 0.95, so a coincidental shared winner is expected;
priority sensitivity shows in the overall ranking).
