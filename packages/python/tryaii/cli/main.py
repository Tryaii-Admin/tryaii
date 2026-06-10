"""
TryAii-DRE CLI.

Commands (kept in parity with the Node SDK's `tryaii`):
    tryaii route "your prompt here"     -- Route a prompt and show recommendations
    tryaii eval prompts.json             -- Route a JSON prompt dataset
    tryaii setup                         -- Pre-generate centroids for faster first use
    tryaii models                        -- List available models
    tryaii benchmarks                    -- List available benchmarks
    tryaii regenerate                    -- Regenerate centroids (after model change)

Global flags: --no-banner, -v/--verbose, -V/--version, -h/--help.

Exit codes (matched with the Node CLI): 0 success, 1 runtime failure,
2 usage error (unknown command/option, missing argument, invalid value).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from html import escape
from pathlib import Path

# Keep byte-identical to HELP in packages/node/src/cli.ts -- both CLIs must
# print the same help text (guarded by tests/test_parity.py).
HELP = """tryaii -- Embedding-based AI model router

Usage:
  tryaii <command> [options]

Commands:
  route <prompt>        Route a prompt to the best model and show recommendations
  eval <input.json>     Route a JSON dataset; writes results.jsonl, summary.json, index.html
  models                List available models (--provider <name>, --json)
  benchmarks            List available benchmarks (--json)
  setup                 Download the embedding model and warm centroids (--model <name>)
  regenerate            Rebuild benchmark centroids, e.g. after changing the embedding model (--model <name>)

Common options:
  --quality <1-5>       Quality priority for route/eval (default 3)
  --cost <1-5>          Cost priority for route/eval (default 3)
  --speed <1-5>         Speed priority for route/eval (default 3)
  --top-k <n>           Number of recommendations (default 5)

Eval-only options:
  -o, --output <dir>    Output directory (default: ./tryaii-eval-<timestamp>)
  --max-price <usd>     Total dataset budget; switches eval to budget-optimized mode
  --output-tokens <n>   Expected output tokens per prompt for budget estimation (default 1000)
  --budget-mode <mode>  'strict' (default) or 'fit-output'
  --difficulty-source <s>  Gauge task complexity: 'intrinsic' (default), 'capability', or 'blend'
  --difficulty-gamma <n>   How hard to shift budget toward complex prompts (default 1; 0 disables)

Global flags:
  --no-banner           Disable the startup banner (also honored via TRYAII_NO_BANNER)
  -v, --verbose         Enable verbose logging
  -V, --version         Print the version and exit
  -h, --help            Show this help

Examples:
  tryaii route "Write a Python function to merge sorted arrays" --quality=5 --cost=1
  tryaii eval prompts.json --output results/run --quality=5 --cost=1 --speed=1
  tryaii eval prompts.json --max-price=0.10 --output-tokens=2000 --budget-mode=fit-output
  tryaii eval prompts.json --max-price=0.50 --difficulty-source=intrinsic --difficulty-gamma=2
"""


def cmd_route(args):
    """Route a prompt and display results."""
    from tryaii import Priorities, Router

    router = Router()

    priorities = Priorities(
        quality=args.quality,
        cost=args.cost,
        speed=args.speed,
    )

    result = router.route(args.prompt, priorities=priorities, top_k=args.top_k)

    print(f"\nPrompt: {args.prompt}")
    print(f"Category: {result.classification.broad_category} > {result.classification.subcategory}")
    print(f"Confidence: {result.classification.confidence:.3f}")
    print(f"Classifier: {result.classification.classifier_used}")
    print(f"\nTop {len(result.scores)} Recommendations:")
    print("-" * 70)

    for i, score in enumerate(result.scores, 1):
        model = router.models.get_model(score.model_id)
        provider = model.provider if model else "?"
        price = ""
        if model and model.pricing:
            price = f"${model.pricing.input_per_1k:.4f}/${model.pricing.output_per_1k:.4f} per 1k"

        print(f"  {i}. {score.model_id}")
        print(f"     Provider: {provider} | Score: {score.final_score:.3f}")
        print(f"     Quality: {score.quality_score:.3f} | Cost: {score.cost_score:.3f} | Speed: {score.speed_score:.3f}")
        if price:
            print(f"     Pricing: {price}")
        print(f"     Reason: {score.reasoning}")
        print()


def _load_eval_prompts(path: Path) -> list[dict]:
    """Load eval rows from an array of strings or objects with a prompt field."""
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, list):
        raise ValueError(f"Expected top-level JSON array in {path}")

    rows = []
    for idx, item in enumerate(data, start=1):
        if isinstance(item, str):
            rows.append({"id": f"p{idx}", "prompt": item, "category": "unknown"})
        elif isinstance(item, dict) and isinstance(item.get("prompt"), str):
            rows.append(
                {
                    "id": str(item.get("id") or f"p{idx}"),
                    "prompt": item["prompt"],
                    "category": str(item.get("category") or "unknown"),
                }
            )
        else:
            raise ValueError(
                f"Item at index {idx - 1} is neither a string nor an object with prompt"
            )
    return rows


def _top_benchmarks(classification, limit: int = 5) -> list[dict]:
    if classification is None:
        return []
    pairs = sorted(
        classification.benchmark_scores.items(),
        key=lambda item: item[1],
        reverse=True,
    )
    return [{"name": name, "score": round(score, 4)} for name, score in pairs[:limit]]


def _route_eval_row(router, row: dict, priorities, top_k: int) -> dict:
    started = time.perf_counter()
    try:
        result = router.route(row["prompt"], priorities=priorities, top_k=top_k)
        classification = result.classification
        return {
            "id": row["id"],
            "category": row["category"],
            "prompt": row["prompt"],
            "bestModel": result.best_model,
            "bestScore": result.best_score,
            "bestReasoning": result.best_reasoning,
            "topK": [
                {"modelId": score.model_id, "finalScore": score.final_score}
                for score in result.scores
            ],
            "topBenchmarks": _top_benchmarks(classification),
            "broadCategory": classification.broad_category if classification else "",
            "subcategory": classification.subcategory if classification else "",
            "confidence": classification.confidence if classification else 0,
            "routeMs": round((time.perf_counter() - started) * 1000, 2),
        }
    except Exception as exc:
        return {
            "id": row["id"],
            "category": row["category"],
            "prompt": row["prompt"],
            "bestModel": "",
            "bestScore": 0,
            "bestReasoning": "",
            "topK": [],
            "topBenchmarks": [],
            "broadCategory": "",
            "subcategory": "",
            "confidence": 0,
            "routeMs": round((time.perf_counter() - started) * 1000, 2),
            "error": str(exc),
        }


def _build_eval_summary(results: list[dict], priorities) -> dict:
    successes = [row for row in results if not row.get("error")]
    total_ms = sum(float(row["routeMs"]) for row in results)
    model_counts = Counter(row["bestModel"] for row in successes)

    distribution = [
        {
            "model": model,
            "count": count,
            "pct": round((count / max(1, len(successes))) * 100, 2),
        }
        for model, count in model_counts.most_common()
    ]

    by_category = defaultdict(list)
    for row in successes:
        by_category[row["category"]].append(row)

    categories = []
    for category, rows in by_category.items():
        cat_models = Counter(row["bestModel"] for row in rows)
        bench_totals: dict[str, float] = defaultdict(float)
        bench_counts: dict[str, int] = defaultdict(int)
        for row in rows:
            for bench in row.get("topBenchmarks") or []:
                name = bench.get("name")
                if not name:
                    continue
                bench_totals[name] += float(bench.get("score", 0))
                bench_counts[name] += 1
        bench_avgs = [
            {"name": name, "avgScore": round(bench_totals[name] / bench_counts[name], 4)}
            for name in bench_totals
        ]
        bench_avgs.sort(key=lambda entry: entry["avgScore"], reverse=True)

        categories.append(
            {
                "category": category,
                "count": len(rows),
                "topModels": [
                    {
                        "model": model,
                        "count": count,
                        "pct": round((count / len(rows)) * 100, 2),
                    }
                    for model, count in cat_models.most_common()
                ],
                "topBenchmarks": bench_avgs[:5],
            }
        )

    return {
        "totalPrompts": len(results),
        "successCount": len(successes),
        "errorCount": len(results) - len(successes),
        "distinctModels": len(model_counts),
        "avgRouteMs": round(total_ms / max(1, len(results)), 2),
        "totalRouteMs": round(total_ms, 2),
        "priorities": priorities.to_dict(),
        "distribution": distribution,
        "byCategory": sorted(categories, key=lambda row: row["count"], reverse=True),
    }


_DASHBOARD_STYLE = """  :root {
    --bg: #0b0d10;
    --panel: #14181d;
    --panel-2: #1b2026;
    --text: #e6e9ee;
    --muted: #8a939d;
    --line: #232932;
    --accent: #6ee7b7;
    --accent-2: #93c5fd;
    --warn: #fcd34d;
  }
  @media (prefers-color-scheme: light) {
    :root { --bg:#fafbfc; --panel:#ffffff; --panel-2:#f4f6f9; --text:#0f1419; --muted:#5b6470; --line:#e6eaef; --accent:#059669; --accent-2:#2563eb; --warn:#b45309; }
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: var(--bg); color: var(--text);
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
  main { max-width: 1100px; margin: 0 auto; padding: 32px 24px 64px; }
  header.top { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 8px; }
  header.top h1 { font-size: 18px; margin: 0; font-weight: 600; letter-spacing: 0.2px; }
  header.top h1 small { color: var(--muted); font-weight: 400; margin-left: 8px; }
  .meta { color: var(--muted); font-size: 12px; }
  .chips { display: flex; gap: 8px; margin: 16px 0 28px; flex-wrap: wrap; }
  .chip { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 999px;
    background: var(--panel-2); border: 1px solid var(--line); font-size: 12px; color: var(--muted); }
  .chip b { color: var(--text); font-weight: 600; }
  .chip-p5 b { color: var(--accent); }
  .chip-p4 b { color: var(--accent-2); }
  .chip-p1 b, .chip-p2 b { color: var(--muted); }
  .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 28px; }
  .stat { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; }
  .stat .k { font-size: 11px; text-transform: uppercase; letter-spacing: 0.6px; color: var(--muted); }
  .stat .v { font-size: 22px; font-weight: 600; margin-top: 4px; }
  .stat .v.warn { color: var(--warn); }
  section { margin-bottom: 32px; }
  section > h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.8px; color: var(--muted);
    font-weight: 600; margin: 0 0 12px; }
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 16px 20px; }
  ul.rows { list-style: none; margin: 0; padding: 0; }
  ul.rows .row { display: grid; grid-template-columns: 1fr 2fr auto auto; gap: 12px; align-items: center;
    padding: 6px 0; border-bottom: 1px dashed var(--line); }
  ul.rows .row:last-child { border-bottom: 0; }
  .row-label { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .row-bar { background: var(--panel-2); border-radius: 4px; height: 8px; overflow: hidden; }
  .row-bar-fill { display: block; height: 100%; background: linear-gradient(90deg, var(--accent), var(--accent-2)); }
  .row-num { color: var(--muted); font-variant-numeric: tabular-nums; min-width: 40px; text-align: right; }
  .row-pct { color: var(--text); font-variant-numeric: tabular-nums; min-width: 56px; text-align: right; font-weight: 500; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; }
  .card-head { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 8px; }
  .card-head h3 { font-size: 14px; margin: 0; font-weight: 600; text-transform: capitalize; }
  .card-sub { font-size: 11px; text-transform: uppercase; letter-spacing: 0.6px; color: var(--muted);
    margin: 12px 0 6px; font-weight: 600; }
  .card .rows .row { grid-template-columns: 1fr 2fr auto; }
  ul.benches { list-style: none; margin: 0; padding: 0; }
  ul.benches li { display: flex; justify-content: space-between; padding: 3px 0; font-size: 12px;
    color: var(--muted); }
  ul.benches b { color: var(--text); font-variant-numeric: tabular-nums; font-weight: 500; }
  .muted { color: var(--muted); font-size: 12px; }
  footer { color: var(--muted); font-size: 12px; margin-top: 32px; display: flex; gap: 16px; flex-wrap: wrap; }
  footer a { color: var(--accent-2); text-decoration: none; }
  footer a:hover { text-decoration: underline; }
  @media (max-width: 720px) { .stats { grid-template-columns: repeat(2, 1fr); } }"""


def _render_eval_dashboard(summary: dict, source: str) -> str:
    """Render a self-contained HTML dashboard for eval results.

    Matches the @tryaii/dre Node dashboard so reports look identical across SDKs.
    """
    priorities = summary["priorities"]
    quality = priorities["quality"]
    cost = priorities["cost"]
    speed = priorities["speed"]
    now = datetime.now(timezone.utc)
    generated_at = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

    def priority_chip(label: str, value: int) -> str:
        return f'<span class="chip chip-p{value}">{label} <b>{value}</b></span>'

    dist_rows = "".join(
        f"""
        <li class="row">
          <span class="row-label" title="{escape(row['model'])}">{escape(row['model'])}</span>
          <span class="row-bar"><span class="row-bar-fill" style="width:{row['pct']}%"></span></span>
          <span class="row-num">{row['count']}</span>
          <span class="row-pct">{row['pct']}%</span>
        </li>"""
        for row in summary["distribution"]
    )

    category_cards = []
    for cat in summary["byCategory"]:
        models_html = "".join(
            f"""
            <li class="row">
              <span class="row-label" title="{escape(m['model'])}">{escape(m['model'])}</span>
              <span class="row-bar"><span class="row-bar-fill" style="width:{m['pct']}%"></span></span>
              <span class="row-pct">{m['pct']}%</span>
            </li>"""
            for m in cat["topModels"][:3]
        )
        benches = cat.get("topBenchmarks") or []
        benches_html = "".join(
            f"<li><span>{escape(b['name'])}</span><b>{b['avgScore']:.3f}</b></li>"
            for b in benches[:5]
        )
        benches_block = (
            f'<h4 class="card-sub">Top benchmarks</h4><ul class="benches">{benches_html}</ul>'
            if benches_html
            else ""
        )
        category_cards.append(
            f"""
        <article class="card">
          <header class="card-head">
            <h3>{escape(cat['category'])}</h3>
            <span class="muted">{cat['count']} prompts</span>
          </header>
          <h4 class="card-sub">Top models</h4>
          <ul class="rows">{models_html}</ul>
          {benches_block}
        </article>"""
        )
    category_cards_html = "".join(category_cards)

    errors_class = " warn" if summary["errorCount"] > 0 else ""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>tryaii eval &mdash; {summary['totalPrompts']} prompts</title>
<style>
{_DASHBOARD_STYLE}
</style>
</head>
<body>
<main>
  <header class="top">
    <h1>tryaii routing eval <small>{summary['totalPrompts']} prompts</small></h1>
    <span class="meta">{escape(generated_at)}</span>
  </header>
  <div class="meta">input: <code>{escape(source)}</code></div>

  <div class="chips">
    {priority_chip('quality', quality)}
    {priority_chip('cost', cost)}
    {priority_chip('speed', speed)}
  </div>

  <div class="stats">
    <div class="stat"><div class="k">Successes</div><div class="v">{summary['successCount']}</div></div>
    <div class="stat"><div class="k">Errors</div><div class="v{errors_class}">{summary['errorCount']}</div></div>
    <div class="stat"><div class="k">Distinct models</div><div class="v">{summary['distinctModels']}</div></div>
    <div class="stat"><div class="k">Avg route</div><div class="v">{summary['avgRouteMs']} <span class="muted" style="font-size:13px">ms</span></div></div>
  </div>

  <section>
    <h2>Recommended models &mdash; overall</h2>
    <div class="panel"><ul class="rows">{dist_rows}</ul></div>
  </section>

  <section>
    <h2>By category</h2>
    <div class="grid">{category_cards_html}</div>
  </section>

  <footer>
    <span>artifacts:</span>
    <a href="summary.json">summary.json</a>
    <a href="results.jsonl">results.jsonl</a>
  </footer>
</main>
</body>
</html>
"""


def cmd_eval(args):
    """Route a JSON prompt dataset and write results.jsonl + summary.json."""
    from tryaii import Priorities, Router
    from tryaii.budget import route_dataset_with_budget

    if args.difficulty_gamma < 0:
        # Usage error -> exit 2, matching both argparse and the Node CLI.
        print("error: --difficulty-gamma must be a non-negative number", file=sys.stderr)
        sys.exit(2)

    input_path = Path(args.input_json).resolve()
    if args.output:
        output_dir = Path(args.output).resolve()
    else:
        stamp = time.strftime("tryaii-eval-%Y%m%d-%H%M%S")
        output_dir = Path.cwd() / stamp

    priorities = Priorities(args.quality, args.cost, args.speed)
    rows = _load_eval_prompts(input_path)

    print(f"[eval] input      : {input_path}")
    print(f"[eval] output     : {output_dir}")
    if args.max_price is None:
        print(
            f"[eval] priorities : quality={priorities.quality} "
            f"cost={priorities.cost} speed={priorities.speed}"
        )
    else:
        print("[eval] objective  : maximize quality under total budget")
        print("[eval] priorities : ignored for budgeted runs")
    print(f"[eval] loaded {len(rows)} prompt(s)")

    router = Router()
    print("[eval] warming up router...")
    router.route("warmup", priorities=priorities, top_k=1)

    budget_summary = None
    if args.max_price is not None:
        print(
            f"[eval] budget     : ${args.max_price:.6f} total, "
            f"{args.output_tokens} output tokens/prompt, mode={args.budget_mode}, "
            f"difficulty={args.difficulty_source}"
        )
        next_progress_pct = 10

        def progress(done: int, total: int) -> None:
            nonlocal next_progress_pct
            progress_pct = int((done / max(1, total)) * 100)
            if progress_pct >= next_progress_pct or done == total:
                print(f"[eval] built candidates {done}/{total} ({min(progress_pct, 100)}%)")
                while next_progress_pct <= progress_pct:
                    next_progress_pct += 10

        budgeted_results, optimization = route_dataset_with_budget(
            router=router,
            prompts=[row["prompt"] for row in rows],
            priorities=priorities,
            max_price=args.max_price,
            output_tokens=args.output_tokens,
            budget_mode=args.budget_mode,
            difficulty_source=args.difficulty_source,
            difficulty_gamma=args.difficulty_gamma,
            progress_callback=progress,
        )
        results = []
        for budgeted in budgeted_results:
            selected = budgeted.selected
            row = rows[selected.prompt_index]
            route_result = budgeted.route_result
            classification = route_result.classification
            results.append(
                {
                    "id": row["id"],
                    "category": row["category"],
                    "prompt": row["prompt"],
                    "bestModel": selected.model_id,
                    "normalBestModel": selected.normal_best_model,
                    "budgetConstrained": selected.model_id != selected.normal_best_model,
                    "bestScore": selected.final_score,
                    "bestReasoning": selected.reasoning,
                    "difficulty": round(selected.difficulty, 4),
                    "estimatedCost": round(selected.estimated_cost, 8),
                    "cumulativeCost": round(budgeted.cumulative_cost, 8),
                    "remainingBudget": round(budgeted.remaining_budget, 8),
                    "inputTokens": selected.input_tokens,
                    "outputTokens": selected.output_tokens,
                    "topK": [
                        {"modelId": score.model_id, "finalScore": score.final_score}
                        for score in route_result.scores[: args.top_k]
                    ],
                    "topBenchmarks": _top_benchmarks(classification),
                    "broadCategory": classification.broad_category if classification else "",
                    "subcategory": classification.subcategory if classification else "",
                    "confidence": classification.confidence if classification else 0,
                    "routeMs": budgeted.route_ms,
                    "optimizerStatus": optimization.status,
                }
            )
        budget_summary = {
            "status": optimization.status,
            "budget": optimization.budget,
            "budgetMode": optimization.budget_mode,
            "difficultySource": args.difficulty_source,
            "selectionObjective": "maximizeQualityUnderBudget",
            "prioritiesIgnored": True,
            "requestedOutputTokens": optimization.requested_output_tokens,
            "effectiveOutputTokens": optimization.effective_output_tokens,
            "outputTokens": optimization.effective_output_tokens,
            "totalEstimatedCost": round(optimization.total_estimated_cost, 8),
            "minimumRequiredBudget": round(optimization.minimum_required_budget, 8)
            if optimization.minimum_required_budget != float("inf")
            else None,
            "requestedMinimumRequiredBudget": round(
                optimization.requested_minimum_required_budget,
                8,
            )
            if optimization.requested_minimum_required_budget is not None
            and optimization.requested_minimum_required_budget != float("inf")
            else None,
            "budgetShortfall": round(optimization.budget_shortfall, 8)
            if optimization.budget_shortfall != float("inf")
            else None,
            "costUnit": optimization.cost_unit,
            "message": optimization.message,
        }
        print(f"[eval] optimizer status: {optimization.status}")
        if (
            optimization.requested_output_tokens is not None
            and optimization.effective_output_tokens is not None
            and optimization.effective_output_tokens != optimization.requested_output_tokens
        ):
            print(
                "[eval] output fit : "
                f"{optimization.requested_output_tokens} -> "
                f"{optimization.effective_output_tokens} tokens/prompt"
            )
    else:
        results = []
        next_progress_pct = 10
        total_rows = len(rows)
        for idx, row in enumerate(rows, start=1):
            results.append(_route_eval_row(router, row, priorities, args.top_k))
            progress_pct = int((idx / max(1, total_rows)) * 100)
            if progress_pct >= next_progress_pct or idx == total_rows:
                print(f"[eval] routed {idx}/{total_rows} ({min(progress_pct, 100)}%)")
                while next_progress_pct <= progress_pct:
                    next_progress_pct += 10

    output_dir.mkdir(parents=True, exist_ok=True)
    results_path = output_dir / "results.jsonl"
    summary_path = output_dir / "summary.json"
    dashboard_path = output_dir / "index.html"

    results_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in results),
        encoding="utf-8",
    )
    summary = _build_eval_summary(results, priorities)
    if budget_summary is not None:
        summary["budget"] = budget_summary
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    dashboard_path.write_text(
        _render_eval_dashboard(summary, str(input_path)),
        encoding="utf-8",
    )

    print("\n[eval] === Summary ===")
    print(f"Prompts        : {summary['totalPrompts']}")
    print(f"Successes      : {summary['successCount']}")
    print(f"Errors         : {summary['errorCount']}")
    print(f"Distinct models: {summary['distinctModels']}")
    print(f"Avg route time : {summary['avgRouteMs']} ms")
    if budget_summary is not None:
        print(f"Budget status  : {budget_summary['status']}")
        print(f"Estimated cost : ${budget_summary['totalEstimatedCost']:.6f}")
        print(f"Budget         : ${budget_summary['budget']:.6f}")
    print("\nTop recommended models:")
    for row in summary["distribution"][:10]:
        print(f"  {row['model']:<40} {row['count']:>5}  ({row['pct']}%)")
    print(f"\n[eval] per-prompt results -> {results_path}")
    print(f"[eval] summary            -> {summary_path}")
    print(f"[eval] dashboard          -> {dashboard_path}")

    # Exit non-zero when every prompt errored so callers/CI can detect a total failure.
    total_prompts = summary["totalPrompts"]
    if total_prompts > 0 and summary["errorCount"] == total_prompts:
        first_error = next(
            (row["error"] for row in results if row.get("error")),
            "all prompts failed to route",
        )
        print(f"[eval] error: all {total_prompts} prompt(s) failed: {first_error}", file=sys.stderr)
        sys.exit(1)


def cmd_setup(args):
    """Pre-generate centroids."""
    from tryaii import TryaiiDreConfig
    from tryaii.centroids.loader import CentroidLoader
    from tryaii.embeddings.local import LocalEmbeddingProvider

    config = TryaiiDreConfig()
    if args.model:
        config.embedding_model = args.model

    print(f"Setting up TryAii-DRE with embedding model: {config.embedding_model}")
    print("This will download the model and load benchmark centroids (one-time operation)...\n")

    provider = LocalEmbeddingProvider(model_name=config.embedding_model)
    loader = CentroidLoader(config=config, embedding_provider=provider)
    centroids = loader.get_centroids()

    print(f"Setup complete! {len(centroids)} benchmark centroids ready.")


def cmd_models(args):
    """List available models."""
    from tryaii import ModelRegistry

    registry = ModelRegistry.default()
    models = registry.all_models

    if args.provider:
        models = [m for m in models if m.provider.lower() == args.provider.lower()]

    if args.json:
        data = [m.to_dict() for m in models]
        print(json.dumps(data, indent=2))
        return

    print(f"\nAvailable Models ({len(models)}):")
    print("-" * 70)

    by_provider: dict[str, list] = {}
    for m in models:
        by_provider.setdefault(m.provider, []).append(m)

    for provider, provider_models in sorted(by_provider.items()):
        print(f"\n  {provider} ({len(provider_models)} models):")
        for m in provider_models:
            latency = m.latency or "?"
            price = ""
            if m.pricing:
                price = f" | ${m.pricing.input_per_1k:.4f}/{m.pricing.output_per_1k:.4f}"
            print(f"    - {m.model_id} [{latency}]{price}")


def cmd_benchmarks(args):
    """List available benchmarks."""
    from tryaii import BenchmarkRegistry

    registry = BenchmarkRegistry.default()

    if args.json:
        data = [b.to_dict() for b in registry.all_benchmarks]
        print(json.dumps(data, indent=2))
        return

    print(f"\nAvailable Benchmarks ({len(registry)}):")
    print("-" * 60)

    for b in registry.all_benchmarks:
        norm = f"[{b.normalization.min_score}-{b.normalization.max_score}]"
        print(f"  {b.name:30s} {norm:15s} {b.description}")


def cmd_regenerate(args):
    """Regenerate centroids."""
    from tryaii import TryaiiDreConfig
    from tryaii.centroids.loader import CentroidLoader
    from tryaii.embeddings.local import LocalEmbeddingProvider

    config = TryaiiDreConfig()
    if args.model:
        config.embedding_model = args.model

    print(f"Regenerating centroids for: {config.embedding_model}")

    provider = LocalEmbeddingProvider(model_name=config.embedding_model)
    loader = CentroidLoader(config=config, embedding_provider=provider)
    centroids = loader.regenerate()

    print(f"Done! Generated {len(centroids)} centroids at {config.centroid_file}")


def cli():
    """Main CLI entry point."""
    # -v/--verbose, --no-banner, -V/--version and -h/--help are handled before
    # argparse runs (see below) so they work in any position, matching the Node
    # CLI; they are intentionally not registered here.
    parser = argparse.ArgumentParser(
        prog="tryaii",
        description="TryAii-DRE -- Embedding-based AI model router",
    )

    subparsers = parser.add_subparsers(dest="command")

    # route
    route_parser = subparsers.add_parser("route", help="Route a prompt to the best model")
    route_parser.add_argument("prompt", help="The prompt to route")
    route_parser.add_argument("--quality", type=int, default=3, help="Quality priority (1-5)")
    route_parser.add_argument("--cost", type=int, default=3, help="Cost priority (1-5)")
    route_parser.add_argument("--speed", type=int, default=3, help="Speed priority (1-5)")
    route_parser.add_argument("--top-k", type=int, default=5, help="Number of recommendations")

    # eval
    eval_parser = subparsers.add_parser("eval", help="Route a JSON prompt dataset")
    eval_parser.add_argument("input_json", help="JSON array of prompts or prompt objects")
    eval_parser.add_argument("-o", "--output", help="Output directory")
    eval_parser.add_argument("--quality", type=int, default=3, help="Quality priority (1-5)")
    eval_parser.add_argument("--cost", type=int, default=3, help="Cost priority (1-5)")
    eval_parser.add_argument("--speed", type=int, default=3, help="Speed priority (1-5)")
    eval_parser.add_argument("--top-k", type=int, default=5, help="Number of recommendations")
    eval_parser.add_argument("--max-price", type=float, help="Global dataset budget in USD")
    eval_parser.add_argument(
        "--output-tokens",
        type=int,
        default=1000,
        help="Expected output tokens per prompt for budget estimation",
    )
    eval_parser.add_argument(
        "--budget-mode",
        choices=("strict", "fit-output"),
        default="strict",
        help="Budget handling: strict fails if requested output tokens do not fit; "
        "fit-output lowers output tokens to keep all prompts under budget",
    )
    eval_parser.add_argument(
        "--difficulty-source",
        choices=("intrinsic", "capability", "blend"),
        default="intrinsic",
        help="Gauge task complexity: intrinsic (default), capability, or blend",
    )
    eval_parser.add_argument(
        "--difficulty-gamma",
        type=float,
        default=1.0,
        help="How hard to shift budget toward complex prompts (default 1; 0 disables)",
    )

    # setup
    setup_parser = subparsers.add_parser("setup", help="Initialize centroids")
    setup_parser.add_argument("--model", help="Embedding model name")

    # models
    models_parser = subparsers.add_parser("models", help="List available models")
    models_parser.add_argument("--provider", help="Filter by provider")
    models_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # benchmarks
    bench_parser = subparsers.add_parser("benchmarks", help="List available benchmarks")
    bench_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # regenerate
    regen_parser = subparsers.add_parser("regenerate", help="Regenerate centroids")
    regen_parser.add_argument("--model", help="Embedding model name")

    raw_args = sys.argv[1:]

    # --version short-circuits everything else (matches the Node CLI).
    if "--version" in raw_args or "-V" in raw_args:
        from tryaii import __version__

        print(__version__)
        return

    # Accept --no-banner and -v/--verbose anywhere (before OR after the
    # subcommand) by stripping them before argparse runs -- argparse would
    # otherwise only honor a global flag that precedes the subcommand.
    # Matches the Node CLI's behavior.
    no_banner = "--no-banner" in raw_args or bool(os.environ.get("TRYAII_NO_BANNER"))
    verbose = "--verbose" in raw_args or "-v" in raw_args
    filtered = [a for a in raw_args if a not in ("--no-banner", "--verbose", "-v")]

    if not no_banner:
        from tryaii.cli import banner

        banner.show()

    # Default to WARNING so normal runs are as quiet as the Node CLI;
    # --verbose opens everything up to DEBUG.
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    if not verbose:
        logging.getLogger("tryaii").setLevel(logging.WARNING)
        logging.getLogger("sentence_transformers").setLevel(logging.WARNING)

    # Like --no-banner/--verbose, help is honored anywhere, including after a
    # subcommand (e.g. `tryaii eval --help`), and as a bare `tryaii help`.
    command = filtered[0] if filtered else None
    if command is None or command == "help" or "-h" in filtered or "--help" in filtered:
        sys.stdout.write(HELP)
        return

    args = parser.parse_args(filtered)

    handlers = {
        "route": cmd_route,
        "eval": cmd_eval,
        "setup": cmd_setup,
        "models": cmd_models,
        "benchmarks": cmd_benchmarks,
        "regenerate": cmd_regenerate,
    }
    try:
        handlers[args.command](args)
    except Exception as exc:
        # Show a clean one-line message instead of a traceback (matches Node).
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    cli()
