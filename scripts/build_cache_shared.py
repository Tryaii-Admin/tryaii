#!/usr/bin/env python3
"""Build a richer `cache-shared/` from the cached tryaii-bench snapshot.

Mirrors the layout of `shared/` (models/, training/, centroids/) but sourced from
`cache/snapshot/*.json` instead of the hand-maintained data. Offline transform —
run `cache/fetch_snapshot.py` first to populate the snapshot (including
`training_queries_full.json`).

Usage:
    python scripts/build_cache_shared.py [--min-models N]

Benchmark selection: a benchmark is kept only if it has curated training-query
text AND is scored for at least --min-models distinct models (default 30).
Models are kept only if they have >=1 score on a kept benchmark.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = REPO_ROOT / "cache" / "snapshot"
OUT = REPO_ROOT / "cache-shared"
EMBED_MODEL = "all-MiniLM-L6-v2"
LONG_CONTEXT_TOKENS = 200_000

# Reconcile server name drift onto a single canonical display name.
ALIASES = {
    "MMLU-PRO": "MMLU-Pro",
}

# Make the python package importable without requiring an editable install.
sys.path.insert(0, str(REPO_ROOT / "packages" / "python"))


def canonical(name: str) -> str:
    return ALIASES.get(name.strip(), name.strip())


def finite_or_none(x: object) -> float | None:
    """Return x as a float, or None for non-numeric / non-finite (NaN/Inf) values."""
    if isinstance(x, (int, float)) and math.isfinite(x):
        return float(x)
    return None


# Snapshot files this build consumes, mapped to the manifest endpoint key that
# must report ok:true for the file to be trustworthy.
REQUIRED_ENDPOINTS = {
    "models.json": "/models",
    "pricing.json": "/pricing",
    "capabilities.json": "/capabilities",
    "benchmarks_static.json": "/benchmarks/static",
    "benchmarks_live.json": "/benchmarks/live",
    "speed.json": "/speed",
    "training_queries_full.json": "/training-queries/*",
}


def load_manifest() -> dict:
    """Read and validate manifest.json.

    The manifest — not file existence — is the source of truth for snapshot
    integrity. A partial fetch leaves stale files paired with fresh ones, so we
    fail loudly if the run was incomplete or any consumed endpoint failed.
    """
    path = SNAPSHOT / "manifest.json"
    if not path.is_file():
        sys.exit(f"ERROR: missing {path}. Run `python cache/fetch_snapshot.py` first.")
    manifest = json.loads(path.read_text(encoding="utf-8"))

    if not manifest.get("fetched_at"):
        sys.exit("ERROR: manifest.json has no fetched_at (stale/corrupt snapshot). "
                 "Re-run `python cache/fetch_snapshot.py`.")

    # A run that did not fetch every endpoint is partial: the files on disk are a
    # mix of fresh and stale and must not be combined.
    succeeded, total = manifest.get("succeeded"), manifest.get("total")
    if succeeded != total:
        sys.exit(f"ERROR: snapshot is partial ({succeeded}/{total} endpoints). "
                 "Re-run `python cache/fetch_snapshot.py`.")

    endpoints = manifest.get("endpoints") or {}
    for filename, key in REQUIRED_ENDPOINTS.items():
        info = endpoints.get(key)
        if not isinstance(info, dict) or not info.get("ok"):
            reason = (info or {}).get("error", "not fetched")
            sys.exit(f"ERROR: required endpoint {key} ({filename}) failed: {reason}. "
                     "Re-run `python cache/fetch_snapshot.py`.")
    return manifest


def load(name: str) -> dict:
    path = SNAPSHOT / name
    if not path.is_file():
        sys.exit(f"ERROR: missing {path}. Run `python cache/fetch_snapshot.py` first.")
    return json.loads(path.read_text(encoding="utf-8"))


def rows(payload: dict) -> list[dict]:
    data = payload.get("data")
    return data if isinstance(data, list) else []


def latency_bucket(tps: float | None) -> str:
    if tps is None:
        return "unknown"
    if tps >= 80:
        return "very fast"
    if tps >= 40:
        return "fast"
    if tps >= 20:
        return "medium"
    return "slow"


def capability_list(cap: dict | None, context_length: int | None) -> list[str]:
    out: list[str] = []
    if cap:
        if cap.get("is_vision_capable"):
            out.append("vision")
        if cap.get("supports_function_calling"):
            out.append("function-calling")
        if cap.get("supports_json_mode"):
            out.append("json-mode")
    if context_length and context_length >= LONG_CONTEXT_TOKENS:
        out.append("long-context")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-models", type=int, default=30,
                    help="min distinct models scored for a benchmark to be kept")
    args = ap.parse_args()
    min_models = args.min_models

    manifest = load_manifest()
    # Carry the snapshot's real provenance into the outputs instead of a literal.
    snapshot_version = manifest.get("snapshot_version") or manifest["fetched_at"]
    fetched_at = manifest["fetched_at"]

    models = rows(load("models.json"))
    pricing = {r["model_id"]: r for r in rows(load("pricing.json"))}
    capabilities = {r["model_id"]: r for r in rows(load("capabilities.json"))}
    static = rows(load("benchmarks_static.json"))
    live = rows(load("benchmarks_live.json"))
    training_full = load("training_queries_full.json")
    speed_rows = rows(load("speed.json"))

    # Merge static + live scores per model; static (HF) wins on collision.
    # Skip non-finite scores on ingest so NaN/Infinity never reaches an output.
    scores: dict[str, dict[str, float]] = defaultdict(dict)
    for r in static + live:
        score = r.get("score")
        if not isinstance(score, (int, float)) or not math.isfinite(score):
            continue
        b = canonical(r["benchmark"])
        scores[r["model_id"]].setdefault(b, score)

    # Per-benchmark distinct-model coverage.
    coverage: dict[str, int] = defaultdict(int)
    for mid, bench_map in scores.items():
        for b in bench_map:
            coverage[b] += 1

    training_canon = {canonical(name): (name, body) for name, body in training_full.items()}

    # Select benchmarks: must have training-query text AND enough coverage.
    selected = {
        b for b in training_canon
        if coverage.get(b, 0) >= min_models and training_canon[b][1].get("queries")
    }

    # Fastest provider per model -> latency. When a row is flagged
    # is_fastest_provider we use THAT provider's tps; only if no provider is
    # flagged for a model do we fall back to the max tps across its rows.
    flagged_tps: dict[str, float] = {}
    max_tps: dict[str, float] = {}
    for r in speed_rows:
        mid, tps = r.get("model_id"), r.get("tokens_per_second")
        if mid is None or not isinstance(tps, (int, float)) or not math.isfinite(tps):
            continue  # skip non-finite tps on ingest (#18)
        if tps > max_tps.get(mid, float("-inf")):
            max_tps[mid] = tps
        if r.get("is_fastest_provider") and tps > flagged_tps.get(mid, float("-inf")):
            flagged_tps[mid] = tps
    fastest_tps = {mid: flagged_tps.get(mid, max_tps[mid]) for mid in max_tps}

    # Build model entries (only models with >=1 selected score).
    out_models = []
    score_counts = []
    for m in models:
        mid = m["model_id"]
        bench_scores = {b: s for b, s in scores.get(mid, {}).items() if b in selected}
        if not bench_scores:
            continue
        cap = capabilities.get(mid)
        ctx = (cap or {}).get("context_length") or m.get("context_length")
        pr = pricing.get(mid, {})
        prompt = finite_or_none(pr.get("prompt_cost_per_1m_tokens"))
        completion = finite_or_none(pr.get("completion_cost_per_1m_tokens"))
        out_models.append({
            "model_id": mid,
            "provider": mid.split("/")[0] if "/" in mid else "unknown",
            "benchmark_scores": dict(sorted(bench_scores.items())),
            "capabilities": capability_list(cap, ctx),
            "pricing": {
                "input_per_1k": round(prompt / 1000, 6) if prompt is not None else None,
                "output_per_1k": round(completion / 1000, 6) if completion is not None else None,
            },
            "latency": latency_bucket(fastest_tps.get(mid)),
            "description": (m.get("description") or "").strip(),
        })
        score_counts.append(len(bench_scores))

    # --- Write models ---
    # allow_nan=False makes a stray NaN/Infinity fail loudly here rather than
    # emit a non-standard JSON token that downstream parsers reject (#18).
    (OUT / "models").mkdir(parents=True, exist_ok=True)
    (OUT / "models" / "default_models.json").write_text(
        json.dumps({
            "version": snapshot_version,
            "updated": fetched_at,
            "generated_from": "cache/snapshot (tryaii-bench API)",
            "models": out_models,
        }, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )

    # --- Write training queries (selected only) ---
    (OUT / "training").mkdir(parents=True, exist_ok=True)
    training_out = {}
    for b in sorted(selected):
        _, body = training_canon[b]
        training_out[b] = {
            "description": body.get("description", ""),
            "queries": body.get("queries", []),
        }
    (OUT / "training" / "training_queries.json").write_text(
        json.dumps({
            "version": snapshot_version,
            "description": "Training queries for benchmark centroids (from tryaii-bench).",
            "benchmarks": training_out,
        }, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )

    # --- Generate centroids (reuse the package pipeline) ---
    from tryaii_dre.centroids.generator import CentroidGenerator
    from tryaii_dre.embeddings.local import LocalEmbeddingProvider

    provider = LocalEmbeddingProvider(model_name=EMBED_MODEL)
    generator = CentroidGenerator(provider)
    centroids = generator.generate({b: training_out[b]["queries"] for b in training_out})
    (OUT / "centroids").mkdir(parents=True, exist_ok=True)
    generator.save(centroids, OUT / "centroids" / f"centroids_{EMBED_MODEL}.json")

    # --- Coverage report ---
    dropped = {}
    for b in sorted(set(coverage) | set(training_canon)):
        if b in selected:
            continue
        has_q = bool(training_canon.get(b, ("", {}))[1].get("queries")) if b in training_canon else False
        cov = coverage.get(b, 0)
        if not has_q:
            dropped[b] = f"no_training_queries (scored for {cov} models)"
        else:
            dropped[b] = f"below_threshold ({cov} < {min_models} models)"
    report = {
        "min_models": min_models,
        "embedding_model": EMBED_MODEL,
        "total_models_in_snapshot": len(models),
        "models_written": len(out_models),
        "avg_scores_per_model": round(sum(score_counts) / len(score_counts), 2) if score_counts else 0,
        "selected_benchmarks": {b: coverage.get(b, 0) for b in sorted(selected)},
        "dropped_benchmarks": dropped,
        "note": (
            "Scores are raw (0-100 accuracy; Arena Elo ~1000-1500; MT-Bench 0-10). "
            "The router's BenchmarkRegistry has no normalization ranges for these new "
            "benchmark names yet — add them before routing on this data."
        ),
    }
    (OUT / "coverage_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )

    print(f"Models written : {len(out_models)}/{len(models)} "
          f"(avg {report['avg_scores_per_model']} scores/model)")
    print(f"Benchmarks kept: {len(selected)} (>= {min_models} models + training queries)")
    print(f"Benchmarks drop: {len(dropped)}")
    print(f"Output          -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
