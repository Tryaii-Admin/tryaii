"""
TryAii CLI.

Commands (kept in parity with the Node SDK's `tryaii`):
    tryaii route "your prompt here"     -- Route a prompt and show recommendations
    tryaii eval prompts.json             -- Route a JSON prompt dataset
    tryaii cachelint input.json          -- Pre-flight prompt-cache analysis
    tryaii setup                         -- Pre-generate centroids for faster first use
    tryaii models                        -- List available models
    tryaii benchmarks                    -- List available benchmarks
    tryaii regenerate                    -- Regenerate centroids (after model change)
    tryaii help [command]                -- Global help, or detailed help for one command

Per-command help is also reachable via `tryaii <command> -h/--help`.
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
  cachelint <input.json>  Analyze prompt-cache readiness before sending (--json, --provider)
  diagnose <verb>       Agent-driven codebase diagnostics: plan, check (see 'tryaii help diagnose')
  designpartner         Enroll as a tryaii design partner (one resumable command)
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
  tryaii eval examples/prompts.json --output results/run --quality=5 --cost=1 --speed=1
  tryaii eval examples/prompts.json --max-price=0.10 --output-tokens=2000 --budget-mode=fit-output
  tryaii eval examples/prompts.json --max-price=0.50 --difficulty-source=intrinsic --difficulty-gamma=2
  tryaii cachelint request.json --json
"""

# Per-command help. Each string must stay byte-identical to the matching
# template literal in packages/node/src/cli.ts (guarded by tests/test_parity.py).
HELP_ROUTE = """tryaii route -- Route one prompt to the best model

Usage:
  tryaii route <prompt> [options]

Classify a prompt with local embeddings and print the top-K model
recommendations. Runs locally -- no API key needed, nothing is called.

Arguments:
  <prompt>              The prompt to route (required)

Options:
  --quality <1-5>       Quality priority (default 3; out-of-range clamped)
  --cost <1-5>          Cost priority (default 3; out-of-range clamped)
  --speed <1-5>         Speed priority (default 3; out-of-range clamped)
  --top-k <n>           Number of recommendations shown (default 5)
  --no-daemon           Route in-process for this call; do not use or start a daemon

Notes:
  Text output only -- there is no --json for route (use 'eval' or the SDK
  for machine-readable output). Scores are relative per call; do not
  compare them across prompts.
  A background daemon keeps the embedding model warm, so only the first
  call pays the multi-second model load. TRYAII_NO_DAEMON=1 disables it
  globally; TRYAII_DAEMON_IDLE=<s> tunes its idle shutdown (default 900).

Examples:
  tryaii route "Write a Python function to merge sorted arrays"
  tryaii route "Summarize this contract" --quality=5 --cost=1

Exit codes:
  0 success, 1 routing/embedding failure, 2 missing prompt or bad flag.

Docs: docs/cli/route.md
"""

HELP_EVAL = """tryaii eval -- Route a JSON prompt dataset

Usage:
  tryaii eval <input.json> [options]

Route every prompt in a JSON file and write three artifacts into the output
directory: results.jsonl (per-prompt), summary.json (aggregate), and a
self-contained index.html dashboard. Runs locally -- no model APIs called.

Two modes:
  priority (default)    Route each prompt independently using your
                        quality/cost/speed weights.
  budget (--max-price)  Jointly maximize quality across the dataset under a
                        total USD budget. Priority weights are ignored.

Arguments:
  <input.json>          JSON array of prompt strings or {id,prompt,category}

Options:
  -o, --output <dir>    Output directory (default ./tryaii-eval-<timestamp>)
  --quality <1-5>       Quality priority (default 3; priority mode only)
  --cost <1-5>          Cost priority (default 3; priority mode only)
  --speed <1-5>         Speed priority (default 3; priority mode only)
  --top-k <n>           Models recorded per row (default 5)
  --max-price <usd>     Total dataset budget; switches eval to budget mode
  --output-tokens <n>   Assumed output tokens per prompt for costing (default 1000)
  --budget-mode <mode>  'strict' (default) or 'fit-output'
  --difficulty-source <s>  'intrinsic' (default), 'capability', or 'blend'
  --difficulty-gamma <n>   Shift budget toward harder prompts (default 1; 0 disables)
  --no-daemon           Route in-process for this call; do not use or start a daemon

Notes:
  A background daemon keeps the embedding model warm, so only the first
  call pays the multi-second model load. TRYAII_NO_DAEMON=1 disables it
  globally; TRYAII_DAEMON_IDLE=<s> tunes its idle shutdown (default 900).

Examples:
  tryaii eval examples/prompts.json --output results/run --quality=5 --cost=1 --speed=1
  tryaii eval examples/prompts.json --max-price=0.10 --output-tokens=2000 --budget-mode=fit-output
  tryaii eval examples/prompts.json --max-price=0.50 --difficulty-source=intrinsic --difficulty-gamma=2

Exit codes:
  0 success (incl. partial per-prompt failures), 1 bad input / warmup / all
  prompts failed, 2 usage error.

Docs: docs/cli/eval/README.md
"""

HELP_MODELS = """tryaii models -- List the model catalog

Usage:
  tryaii models [options]

Print every model in the default registry, grouped by provider. Each line
shows: model_id [latency-tier] | $input/output per 1k tokens (price omitted
when the model has no pricing).

Options:
  --provider <name>     Filter to one provider (case-insensitive exact match)
  --json                Print the (filtered) models as pretty-printed JSON

Examples:
  tryaii models
  tryaii models --provider anthropic
  tryaii models --json

Exit codes:
  0 success, 1 runtime failure, 2 bad flag.

Docs: docs/cli/models.md
"""

HELP_BENCHMARKS = """tryaii benchmarks -- List registered benchmarks

Usage:
  tryaii benchmarks [options]

Print the standard benchmarks the router scores prompts against. Each line
shows the benchmark name, its normalization range, and a description.

Options:
  --json                Print the benchmarks as pretty-printed JSON

Examples:
  tryaii benchmarks
  tryaii benchmarks --json

Exit codes:
  0 success, 1 runtime failure, 2 bad flag.

Docs: docs/cli/benchmarks.md
"""

HELP_SETUP = """tryaii setup -- Download the embedding model and warm centroids

Usage:
  tryaii setup [options]

One-time initialization: download the embedding model and load or generate
the benchmark centroids so the first real route/eval is fast. Optional --
the same work happens lazily on first use.

Options:
  --model <name>        Embedding model name (default all-MiniLM-L6-v2)

Examples:
  tryaii setup
  tryaii setup --model all-mpnet-base-v2

Exit codes:
  0 success, 1 runtime failure, 2 bad flag.

Docs: docs/cli/setup.md
"""

HELP_REGENERATE = """tryaii regenerate -- Rebuild benchmark centroids

Usage:
  tryaii regenerate [options]

Force-regenerate the benchmark centroids from the bundled training queries,
overwriting the user cache. Unlike setup (which only builds what is missing),
regenerate always rebuilds -- use it after changing the embedding model.

Options:
  --model <name>        Embedding model to generate with (default all-MiniLM-L6-v2)

Examples:
  tryaii regenerate
  tryaii regenerate --model all-mpnet-base-v2

Exit codes:
  0 success, 1 runtime failure, 2 bad flag.

Docs: docs/cli/regenerate.md
"""

HELP_CACHELINT = """tryaii cachelint -- Pre-flight prompt-cache analysis

Usage:
  tryaii cachelint <input.json | -> [options]

Analyze prompts BEFORE they are sent: 18 dynamic-content detectors, per-model
token floors for 7 providers, stable-prefix computation, and predicted
HIT/PARTIAL/MISS across request sequences. Runs locally -- nothing is called.

Arguments:
  <input.json>          Request JSON: an object with "prompt" and "llm"
                        ({"provider": ..., "name": ...}), a list of those, or
                        {"inputs": [...]}. Use '-' to read from stdin.

Options:
  --provider <name>     Raw-text mode: treat the ENTIRE input as one prompt
                        string for this provider (openai, anthropic, gemini,
                        xai, openrouter, bedrock, vertex -- aliases accepted)
  --model <name>        Model name for raw-text mode (requires --provider;
                        omit to use the provider's conservative default floor)
  --json                Emit the full machine-readable result instead of the
                        text report

Notes:
  cachelint warns, it never blocks: findings do not change the exit code.
  Exact OpenAI/xAI token counts use the o200k tokenizer -- install the extra
  on Python ('pip install tryaii[cachelint]'); the Node SDK bundles it.
  Thresholds/prices are time-sensitive; verify against live provider docs.

Examples:
  tryaii cachelint request.json
  tryaii cachelint requests.json --json
  cat prompt.txt | tryaii cachelint - --provider anthropic --model claude-fable-5

Exit codes:
  0 analysis completed (findings included), 1 runtime failure, 2 usage error
  or invalid input.

Docs: docs/cli/cachelint.md
"""

HELP_DIAGNOSE = """tryaii diagnose -- Analyze a codebase's LLM call sites

Usage:
  tryaii diagnose <verb> [options]

diagnose is agent-first: your coding agent interviews you, finds the LLM
call sites in the codebase, and writes an inventory JSON; tryaii runs
deterministic checks over it and stores each run under .tryaii/diagnose/.
Insight-only -- it never edits code and never sends anything anywhere.

Verbs:
  init                  Install the agent playbook into this repo (skill + AGENTS.md)
  plan                  Print the check catalog + interview for the agent (--json)
  check <inventory>     Run the checks over an agent-written inventory JSON
  report                Render a run's findings to a self-contained index.html

The four checks: model_fit (is each call site's model the right one for its
prompt under your priorities), cache_readiness (will the prompt hit the
provider's cache), cost_exposure (per-call/monthly cost, cache savings,
cheaper-swap suggestion), hygiene (prompt structure and dynamic-value
placement).

Examples:
  tryaii diagnose init
  tryaii diagnose plan --json
  tryaii diagnose check inventory.json --quality 3 --cost 4 --speed 2
  tryaii diagnose report

Exit codes:
  0 checks completed (findings included), 1 runtime failure, 2 usage error.

Docs: docs/cli/diagnose/README.md
"""

HELP_DIAGNOSE_INIT = """tryaii diagnose init -- Install the agent playbook into a repo

Usage:
  tryaii diagnose init [options]

Writes the pieces your coding agent needs to run diagnose end to end:

  .claude/skills/tryaii-diagnose/SKILL.md   the playbook (interview ->
                                            discovery -> inventory ->
                                            check -> report)
  AGENTS.md                                 a short pointer block (added
                                            between tryaii-diagnose
                                            markers; created if missing)
  .gitignore                                an anchored /.tryaii/ entry so
                                            run data stays untracked

Idempotent: files already up to date are left alone (the AGENTS.md block
is replaced in place on upgrades). Everything outside the marker block is
never touched.

Options:
  --dir <path>          Target repo root (default: current directory)
  --no-gitignore        Do not touch .gitignore

Examples:
  tryaii diagnose init
  tryaii diagnose init --dir ../my-app --no-gitignore

Exit codes:
  0 success, 1 runtime failure, 2 usage error.

Docs: docs/cli/diagnose/init.md
"""

HELP_DIAGNOSE_PLAN = """tryaii diagnose plan -- The check catalog + interview for the agent

Usage:
  tryaii diagnose plan [--json]

Prints what diagnose can check, the interview questions the agent should
ask the user (checks, scope, priorities, traffic, goal), and the inventory
shape the agent must produce. --json emits the machine-readable plan
(schema tryaii.diagnose.plan/1) including a complete inventory example --
agents should consume that.

Options:
  --json                Emit the machine-readable plan verbatim

Examples:
  tryaii diagnose plan
  tryaii diagnose plan --json

Exit codes:
  0 success, 2 usage error.

Docs: docs/cli/diagnose/plan.md
"""

HELP_DIAGNOSE_CHECK = """tryaii diagnose check -- Run the checks over an inventory JSON

Usage:
  tryaii diagnose check <inventory.json | -> [options]

Reads an agent-written inventory of LLM call sites (see 'diagnose plan
--json' for the shape), runs the selected checks, and writes the run to
<out-dir>/<run-id>/ (inventory.json, findings.json, meta.json) plus a
'latest' pointer. Sites with missing data degrade honestly per check
('insufficient data' with a reason) -- nothing is guessed.

Requires 'tryaii setup' once beforehand when live classification is needed
(any site without a precomputed _classification).

Arguments:
  <inventory.json>      Inventory file, or '-' for stdin

Options:
  --quality <1-5>       Quality priority (default 3)
  --cost <1-5>          Cost priority (default 3)
  --speed <1-5>         Speed priority (default 3)
  --checks <list>       Comma-separated subset of model_fit, cache_readiness,
                        cost_exposure, hygiene (default: all)
  --calls-per-day <n>   Default traffic assumption for sites without one
  --output-tokens <n>   Default output tokens per call (default 500)
  --goal <text>         The user's stated goal (echoed into the findings)
  --out-dir <dir>       Run store directory (default .tryaii/diagnose)
  --run-id <id>         Override the run id (default: UTC timestamp)
  --now <iso8601>       Override the generated_at timestamp
  --json                Print the findings JSON to stdout instead of the summary
  --no-daemon           Classify in-process; do not use or start a daemon

Notes:
  diagnose warns, it never blocks: findings do not change the exit code.
  Cost figures are estimates; cache savings are an upper bound.

Examples:
  tryaii diagnose check inventory.json --quality 3 --cost 4 --speed 2
  tryaii diagnose check inventory.json --calls-per-day 1000 --goal "reduce prices"
  cat inventory.json | tryaii diagnose check - --json

Exit codes:
  0 checks completed (findings included), 1 runtime failure, 2 usage error
  or invalid input.

Docs: docs/cli/diagnose/check.md
"""

HELP_DIAGNOSE_REPORT = """tryaii diagnose report -- Render a run to a self-contained HTML page

Usage:
  tryaii diagnose report [options]

Renders <out-dir>/<run-id>/findings.json into index.html next to it: check
chips per site (green = healthy), monthly cost/savings tiles, expandable
detail per check, and -- when a previous run exists -- a delta band
("since <run>: N improved..."). A pure function of the stored findings;
the page is self-contained and everything stays local.

Options:
  --run <id>            Run to render (default: the 'latest' pointer)
  --out-dir <dir>       Run store directory (default .tryaii/diagnose)
  --out <file>          Write the HTML somewhere else instead

Examples:
  tryaii diagnose report
  tryaii diagnose report --run 20260814T101530Z

Exit codes:
  0 success, 1 no runs found / runtime failure, 2 usage error.

Docs: docs/cli/diagnose/report.md
"""

HELP_DESIGNPARTNER = """tryaii designpartner -- Enroll as a tryaii design partner

Usage:
  tryaii designpartner [options]

ONE resumable command -- no verbs. Every run reads the enrollment state
(.tryaii/designpartner/), ingests whatever you pass, advances, and prints
the current stage plus exactly what to do next (--json for agents). The
flow: questionnaire -> diagnose run (required for insight tiers) ->
consent -> confirm -> submitted. Your coding agent drives it via the
tryaii-designpartner skill, installed automatically on the first run.

Nothing is EVER sent without an explicit --confirm, and every submission
is written to .tryaii/designpartner/ before any network attempt. Three
consent tiers decide what is shared: contact_only (questionnaire answers
only), summary_insights (adds the redacted diagnose summary -- no code,
no paths, no prompts), full_partnership (adds the full findings AND your
raw prompts -- stated verbatim in its consent copy).

Options:
  --answers <file|->    Validate + save questionnaire answers (JSON object)
  --consent <tier>      Choose a consent tier; writes preview.json
  --confirm             Send the previewed submission (saved locally first)
  --reset               Clear the enrollment state (submissions are kept)
  --json                Print the machine-readable status report
  --out-dir <dir>       State directory (default .tryaii/designpartner)
  --no-gitignore        First run: do not touch .gitignore
  --now <iso8601>       Override timestamps (testing seam)
  --stamp <id>          Override the submission filename stamp (testing seam)

At most one of --answers/--consent/--confirm/--reset per invocation.
The endpoint (https://api.tryaii.com/v1/design-partners) can be overridden
via TRYAII_DESIGNPARTNER_URL. If it cannot be reached, the submission
stays saved locally and the command still succeeds.

Examples:
  tryaii designpartner
  tryaii designpartner --answers answers.json
  tryaii designpartner --consent summary_insights
  tryaii designpartner --confirm

Exit codes:
  0 stage reported (including rejected answers), 1 runtime failure,
  2 usage error.

Docs: docs/cli/designpartner.md
"""

HELP_HELP = """tryaii help -- Show help for tryaii or a specific command

Usage:
  tryaii help [command]
  tryaii <command> --help

With no argument, prints the global overview. With a command name, prints
detailed help for that command. The flags -h/--help after any command do
the same thing.

Topics:
  route, eval, cachelint, diagnose, designpartner, models, benchmarks, setup,
  regenerate, help

Examples:
  tryaii help
  tryaii help eval
  tryaii route --help

Exit codes:
  0 success, 2 unknown help topic.

Docs: docs/cli/README.md
"""

# Per-command help, keyed by command name. Mirrors COMMAND_HELP in the Node CLI.
COMMAND_HELP = {
    "route": HELP_ROUTE,
    "eval": HELP_EVAL,
    "cachelint": HELP_CACHELINT,
    "diagnose": HELP_DIAGNOSE,
    "designpartner": HELP_DESIGNPARTNER,
    "models": HELP_MODELS,
    "benchmarks": HELP_BENCHMARKS,
    "setup": HELP_SETUP,
    "regenerate": HELP_REGENERATE,
    "help": HELP_HELP,
}

# Per-verb help for the diagnose command. Mirrors DIAGNOSE_VERB_HELP in the
# Node CLI (same parity guard as COMMAND_HELP).
DIAGNOSE_VERB_HELP = {
    "init": HELP_DIAGNOSE_INIT,
    "plan": HELP_DIAGNOSE_PLAN,
    "check": HELP_DIAGNOSE_CHECK,
    "report": HELP_DIAGNOSE_REPORT,
}

# Per-line delay (seconds) when revealing human-readable output interactively.
_LINE_DELAY = 0.022


def _write_paced(text: str) -> None:
    """Write human-readable text, revealing it line-by-line at a controlled pace.

    Mirrors ``writePaced`` in the Node CLI: paces the output when stdout is an
    interactive terminal, but dumps instantly (no delay) when stdout is
    piped/redirected or when the banner is suppressed, so scripted use,
    ``--json``, and ``--no-banner`` stay snappy and clean.

    Used for help screens and command results (route/models/benchmarks/eval
    summary). Live progress lines and operational logs are printed directly so
    they appear in real time.
    """
    animate = bool(getattr(sys.stdout, "isatty", lambda: False)()) and not os.environ.get(
        "TRYAII_NO_BANNER"
    )
    if not animate:
        sys.stdout.write(text)
        return
    lines = text.split("\n")
    for i, line in enumerate(lines):
        sys.stdout.write(line + "\n" if i < len(lines) - 1 else line)
        sys.stdout.flush()
        time.sleep(_LINE_DELAY)


def _acquire_route_fn(config, no_daemon: bool):
    """Return (route_fn, source) where route_fn(prompt, priorities, top_k) -> RouteResult.

    Prefers a warm background daemon (auto-starting one if needed) so repeated
    CLI calls skip the embedding-model load. Falls back to an in-process Router
    when the daemon is disabled, unavailable, or fails to start in time.
    """
    from tryaii import daemon as daemon_mod

    if not no_daemon and not daemon_mod.is_disabled():
        def _notice():
            print(
                "[tryaii] starting routing daemon (first run loads the embedding "
                "model, this can take a minute)...",
                file=sys.stderr,
                flush=True,
            )

        try:
            state = daemon_mod.ensure_daemon(config, on_starting=_notice)
        except Exception as exc:  # noqa: BLE001 -- never let daemon issues break routing
            logging.getLogger("tryaii").warning("daemon unavailable, routing in-process: %s", exc)
            state = None
        if state is not None:
            return (lambda prompt, priorities, top_k: daemon_mod.route(
                state, prompt, priorities, top_k)), "daemon"

    from tryaii import Router

    router = Router(config=config)
    return (lambda prompt, priorities, top_k: router.route(
        prompt, priorities=priorities, top_k=top_k)), "inprocess"


def cmd_route(args):
    """Route a prompt and display results."""
    from tryaii import Priorities
    from tryaii.config import TryaiiDreConfig
    from tryaii.registry.models import ModelRegistry

    config = TryaiiDreConfig()
    priorities = Priorities(
        quality=args.quality,
        cost=args.cost,
        speed=args.speed,
    )

    route_fn, _source = _acquire_route_fn(config, getattr(args, "no_daemon", False))
    result = route_fn(args.prompt, priorities, args.top_k)

    # Provider/pricing for display come from the (cheap, torch-free) model
    # registry so the daemon path doesn't need to ship them over the wire.
    registry = ModelRegistry.default()

    buf = f"\nPrompt: {args.prompt}\n"
    buf += f"Category: {result.classification.broad_category} > {result.classification.subcategory}\n"
    buf += f"Confidence: {result.classification.confidence:.3f}\n"
    buf += f"Classifier: {result.classification.classifier_used}\n"
    buf += f"\nTop {len(result.scores)} Recommendations:\n"
    buf += "-" * 70 + "\n"

    for i, score in enumerate(result.scores, 1):
        model = registry.get_model(score.model_id)
        provider = model.provider if model else "?"
        price = ""
        if model and model.pricing:
            price = f"${model.pricing.input_per_1k:.4f}/${model.pricing.output_per_1k:.4f} per 1k"

        buf += f"  {i}. {score.model_id}\n"
        buf += f"     Provider: {provider} | Score: {score.final_score:.3f}\n"
        buf += f"     Quality: {score.quality_score:.3f} | Cost: {score.cost_score:.3f} | Speed: {score.speed_score:.3f}\n"
        if price:
            buf += f"     Pricing: {price}\n"
        buf += f"     Reason: {score.reasoning}\n"
        buf += "\n"

    _write_paced(buf)


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


def _route_eval_row(route_fn, row: dict, priorities, top_k: int) -> dict:
    started = time.perf_counter()
    try:
        result = route_fn(row["prompt"], priorities, top_k)
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
    from tryaii.config import TryaiiDreConfig

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

    config = TryaiiDreConfig()
    budget_summary = None
    if args.max_price is not None:
        # Budget optimization drives the scoring engine directly, so it needs a
        # real in-process Router rather than the daemon's route() surface.
        router = Router(config=config)
        print("[eval] warming up router...")
        router.route("warmup", priorities=priorities, top_k=1)
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
        route_fn, _source = _acquire_route_fn(config, getattr(args, "no_daemon", False))
        print("[eval] warming up router...")
        route_fn("warmup", priorities, 1)
        results = []
        next_progress_pct = 10
        total_rows = len(rows)
        for idx, row in enumerate(rows, start=1):
            results.append(_route_eval_row(route_fn, row, priorities, args.top_k))
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

    buf = "\n[eval] === Summary ===\n"
    buf += f"Prompts        : {summary['totalPrompts']}\n"
    buf += f"Successes      : {summary['successCount']}\n"
    buf += f"Errors         : {summary['errorCount']}\n"
    buf += f"Distinct models: {summary['distinctModels']}\n"
    buf += f"Avg route time : {summary['avgRouteMs']} ms\n"
    if budget_summary is not None:
        buf += f"Budget status  : {budget_summary['status']}\n"
        buf += f"Estimated cost : ${budget_summary['totalEstimatedCost']:.6f}\n"
        buf += f"Budget         : ${budget_summary['budget']:.6f}\n"
    buf += "\nTop recommended models:\n"
    for row in summary["distribution"][:10]:
        buf += f"  {row['model']:<40} {row['count']:>5}  ({row['pct']}%)\n"
    buf += f"\n[eval] per-prompt results -> {results_path}\n"
    buf += f"[eval] summary            -> {summary_path}\n"
    buf += f"[eval] dashboard          -> {dashboard_path}\n"
    _write_paced(buf)

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

    print(f"Setting up TryAii with embedding model: {config.embedding_model}")
    print("This will download the model and load benchmark centroids (one-time operation)...\n")

    provider = LocalEmbeddingProvider(model_name=config.embedding_model)
    loader = CentroidLoader(config=config, embedding_provider=provider)
    centroids = loader.get_centroids()

    # Marker consumed by `diagnose check` (its setup gate): live classification
    # is only allowed once setup has completed at least once on this machine.
    marker = {
        "embedding_model": config.embedding_model,
        "centroid_count": len(centroids),
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    Path(config.data_dir).mkdir(parents=True, exist_ok=True)
    (Path(config.data_dir) / "setup.json").write_text(
        json.dumps(marker, indent=2) + "\n", encoding="utf-8")

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

    buf = f"\nAvailable Models ({len(models)}):\n"
    buf += "-" * 70 + "\n"

    by_provider: dict[str, list] = {}
    for m in models:
        by_provider.setdefault(m.provider, []).append(m)

    for provider, provider_models in sorted(by_provider.items()):
        buf += f"\n  {provider} ({len(provider_models)} models):\n"
        for m in provider_models:
            latency = m.latency or "?"
            price = ""
            if m.pricing:
                price = f" | ${m.pricing.input_per_1k:.4f}/{m.pricing.output_per_1k:.4f}"
            buf += f"    - {m.model_id} [{latency}]{price}\n"

    _write_paced(buf)


def cmd_benchmarks(args):
    """List available benchmarks."""
    from tryaii import BenchmarkRegistry

    registry = BenchmarkRegistry.default()

    if args.json:
        data = [b.to_dict() for b in registry.all_benchmarks]
        print(json.dumps(data, indent=2))
        return

    buf = f"\nAvailable Benchmarks ({len(registry)}):\n"
    buf += "-" * 60 + "\n"

    for b in registry.all_benchmarks:
        norm = f"[{b.normalization.min_score}-{b.normalization.max_score}]"
        buf += f"  {b.name:30s} {norm:15s} {b.description}\n"

    _write_paced(buf)


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


def cmd_cachelint(args):
    """Pre-flight prompt-cache analysis (see shared/cachelint/SPEC.md §4)."""
    if args.model and not args.provider:
        print("error: --model requires --provider (raw-text mode)", file=sys.stderr)
        sys.exit(2)

    if args.input == "-":
        raw = sys.stdin.read()
    else:
        path = Path(args.input)
        if not path.is_file():
            raise FileNotFoundError(f"file not found: {args.input}")
        # utf-8-sig strips a BOM; text mode normalizes \r\n (Node CLI mirrors both).
        raw = path.read_text(encoding="utf-8-sig")

    from tryaii.cachelint import analyze, render_report

    if args.provider:
        # Raw-text mode: the ENTIRE input is one prompt string, never parsed as JSON.
        data = {"prompt": raw,
                "llm": {"provider": args.provider, "name": args.model or ""}}
    else:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Parser-neutral message (SPEC.md delta n): json and JSON.parse differ.
            where = "on stdin" if args.input == "-" else f"in '{args.input}'"
            print(f"error: invalid JSON {where}", file=sys.stderr)
            sys.exit(2)

    try:
        result = analyze(data)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)

    # The report contains em-dashes; Windows consoles may not default to UTF-8.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 -- best effort; never break the report
        pass

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _write_paced(render_report(result) + "\n")


# ---------------------------------------------------------------------------
# diagnose (see shared/diagnose/SPEC.md)
# ---------------------------------------------------------------------------

def _diagnose_data_path(name: str) -> Path:
    from tryaii import diagnose as diagnose_pkg

    return Path(diagnose_pkg.__file__).parent / "data" / name


def _diagnose_plan(argv):
    parser = argparse.ArgumentParser(prog="tryaii diagnose plan", add_help=False)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    raw = _diagnose_data_path("plan.json").read_text(encoding="utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 -- best effort; never break output
        pass
    if args.json:
        # Verbatim bytes of the bundled plan -- parity by construction.
        sys.stdout.write(raw)
        return

    plan = json.loads(raw)
    buf = "tryaii diagnose plan -- what diagnose can check\n\n"
    buf += "Checks (recommended: run all):\n"
    for check in plan["checks"]:
        buf += f"  - {check['id']}: {check['what']}\n"
    buf += "\nInterview -- ask the user:\n"
    for question in plan["interview"]:
        buf += f"  - {question['ask']}\n"
    inv = plan["inventory"]
    buf += ("\nInventory (per site) -- required: "
            + ", ".join(inv["required_per_site"])
            + "; optional: " + ", ".join(inv["optional_per_site"]) + "\n")
    buf += ("Machine-readable plan with the full schema and an example: "
            "tryaii diagnose plan --json\n")
    buf += "\nNext:\n"
    buf += f"  {plan['commands']['check']}\n"
    buf += f"  {plan['commands']['report']}\n"
    _write_paced(buf)


def _diagnose_read_inventory(input_arg: str):
    """Read + parse the inventory argument ('-' = stdin). Mirrors cachelint."""
    if input_arg == "-":
        raw = sys.stdin.read()
    else:
        path = Path(input_arg)
        if not path.is_file():
            raise FileNotFoundError(f"file not found: {input_arg}")
        raw = path.read_text(encoding="utf-8-sig")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Parser-neutral message: json and JSON.parse phrase errors differently.
        where = "on stdin" if input_arg == "-" else f"in '{input_arg}'"
        print(f"error: invalid JSON {where}", file=sys.stderr)
        sys.exit(2)


def _diagnose_money(value) -> str:
    return f"${value:.2f}"


def _diagnose_summary_text(findings: dict, out_dir_display: str) -> str:
    """Human summary of a check run -- byte-identical across both CLIs."""
    summary = findings["summary"]
    skipped = len(findings["inventory"]["skipped"])
    skipped_note = f", {skipped} skipped" if skipped else ""
    buf = f"diagnose: {summary['site_count']} site(s) analyzed{skipped_note}\n\n"
    for check, counts in summary["check_status_counts"].items():
        buf += (f"  {check:<16} {counts['ok']} ok | {counts['finding']} finding | "
                f"{counts['insufficient_data']} insufficient | "
                f"{counts['skipped']} skipped\n")
    totals = summary["totals"]
    if totals["sites_with_traffic_data"] > 0:
        buf += (f"\nmonthly estimates ({totals['sites_with_traffic_data']} "
                "site(s) with traffic data):\n")
        if totals["est_monthly_cost_usd"] is not None:
            buf += f"  est. cost           {_diagnose_money(totals['est_monthly_cost_usd'])}\n"
        if totals["est_monthly_cache_savings_usd"] is not None:
            buf += ("  cache savings (max) "
                    f"{_diagnose_money(totals['est_monthly_cache_savings_usd'])}\n")
        if totals["est_monthly_swap_savings_usd"] is not None:
            buf += f"  swap savings        {_diagnose_money(totals['est_monthly_swap_savings_usd'])}\n"
    else:
        buf += ("\nmonthly estimates: no traffic data "
                "(pass --calls-per-day or per-site calls_per_day)\n")
    run_id = findings["run_id"]
    buf += "\n"
    for name in ("inventory.json", "findings.json", "meta.json"):
        buf += f"-> {out_dir_display}/{run_id}/{name}\n"
    return buf


def _diagnose_check(argv):
    parser = argparse.ArgumentParser(prog="tryaii diagnose check", add_help=False)
    parser.add_argument("input")
    parser.add_argument("--quality", type=int, default=3)
    parser.add_argument("--cost", type=int, default=3)
    parser.add_argument("--speed", type=int, default=3)
    parser.add_argument("--checks")
    parser.add_argument("--calls-per-day", type=float, dest="calls_per_day")
    parser.add_argument("--output-tokens", type=int, dest="output_tokens")
    parser.add_argument("--goal")
    parser.add_argument("--out-dir", default=".tryaii/diagnose", dest="out_dir")
    parser.add_argument("--run-id", dest="run_id")
    parser.add_argument("--now")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-daemon", action="store_true", dest="no_daemon")
    args = parser.parse_args(argv)

    from tryaii import __version__
    from tryaii.diagnose import DEFAULT_CHECKS, analyze_inventory, write_run

    checks = None
    if args.checks is not None:
        checks = [c.strip() for c in args.checks.split(",") if c.strip()]
        bad = [c for c in checks if c not in DEFAULT_CHECKS]
        if bad:
            print(f"error: unknown check '{bad[0]}'. Valid checks: "
                  + ", ".join(DEFAULT_CHECKS), file=sys.stderr)
            sys.exit(2)
        if not checks:
            print("error: --checks selected nothing. Valid checks: "
                  + ", ".join(DEFAULT_CHECKS), file=sys.stderr)
            sys.exit(2)

    data = _diagnose_read_inventory(args.input)

    # Live classification is needed only when model_fit is selected and at
    # least one prompt-bearing site lacks the _classification seam.
    from tryaii.diagnose.intake import normalize_inventory

    norm = normalize_inventory(data)  # raises ValueError -> exit 1 below
    needs_routing = (
        (checks is None or "model_fit" in checks)
        and any(site["prompt"] is not None and site["classification"] is None
                for site in norm["sites"])
    )

    classify_fn = None
    if needs_routing:
        from tryaii.config import TryaiiDreConfig

        config = TryaiiDreConfig()
        if not (Path(config.data_dir) / "setup.json").is_file():
            print("error: diagnose requires setup: run 'tryaii setup' first "
                  "(downloads the embedding model and warms centroids)",
                  file=sys.stderr)
            sys.exit(1)

        from tryaii import Priorities
        from tryaii.classifiers.base import MAX_PROMPT_LENGTH

        priorities_obj = Priorities(
            quality=args.quality, cost=args.cost, speed=args.speed)
        route_fn, _source = _acquire_route_fn(config, args.no_daemon)

        def classify_fn(canonical):
            result = route_fn(canonical[:MAX_PROMPT_LENGTH], priorities_obj, 1)
            c = result.classification
            if c is None:
                return None
            return {
                "benchmark_similarities": dict(c.benchmark_scores),
                "broad_category": c.broad_category,
                "subcategory": c.subcategory,
                "confidence": c.confidence,
            }

    run_id = args.run_id or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    now = args.now or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    findings = analyze_inventory(
        data,
        {
            "run_id": run_id,
            "now": now,
            "version": __version__,
            "priorities": {"quality": args.quality, "cost": args.cost,
                           "speed": args.speed},
            "goal": args.goal,
            "checks": checks,
            "calls_per_day": args.calls_per_day,
            "output_tokens": args.output_tokens,
        },
        classify_fn=classify_fn,
    )

    write_run(Path(args.out_dir), data, findings)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 -- best effort; never break output
        pass
    if args.json:
        print(json.dumps(findings, indent=2, ensure_ascii=False))
    else:
        out_dir_display = args.out_dir.replace("\\", "/")
        _write_paced(_diagnose_summary_text(findings, out_dir_display))


_AGENTS_BEGIN = "<!-- tryaii-diagnose:begin -->"
_AGENTS_END = "<!-- tryaii-diagnose:end -->"
_GITIGNORE_LINE = "/.tryaii/"


def _write_if_changed(path: Path, content: str) -> str:
    """Write `content` if the file differs; returns 'written' or
    'up_to_date'. All init writes are LF-normalized (both CLIs read+write
    LF for parity)."""
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return "up_to_date"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    return "written"


def _install_skill_files(base: Path, base_display: str, skill_data: dict,
                         skill_subdir: str, agents_begin: str, agents_end: str,
                         gitignore_comment: str, no_gitignore: bool) -> list:
    """Install a skill + AGENTS.md marker block + anchored gitignore entry.

    Shared by `diagnose init` and the designpartner first run (each with
    its own markers; both blocks coexist in AGENTS.md). Returns
    [{"path": display, "action": "written"|"up_to_date"}] in write order.
    """
    def display(rel: str) -> str:
        return rel if base_display == "." else f"{base_display}/{rel}"

    results = []

    # 1. The skill (a tryaii-owned file: always safe to overwrite).
    skill_rel = f".claude/skills/{skill_subdir}/SKILL.md"
    action = _write_if_changed(
        base / ".claude" / "skills" / skill_subdir / "SKILL.md",
        skill_data["skill_md"])
    results.append({"path": display(skill_rel), "action": action})

    # 2. AGENTS.md pointer block (replace between markers / append / create;
    #    everything outside the markers is never touched).
    block = skill_data["agents_pointer_md"].strip()
    agents_path = base / "AGENTS.md"
    if agents_path.is_file():
        text = agents_path.read_text(encoding="utf-8")
        if agents_begin in text and agents_end in text:
            start = text.index(agents_begin)
            end = text.index(agents_end) + len(agents_end)
            content = text[:start] + block + text[end:]
        else:
            content = text.rstrip("\n") + "\n\n" + block + "\n"
    else:
        content = block + "\n"
    action = _write_if_changed(agents_path, content)
    results.append({"path": display("AGENTS.md"), "action": action})

    # 3. Anchored gitignore entry (the 0.2.0 wheel incident is why this is
    #    anchored: an unanchored pattern can eat package directories).
    if not no_gitignore:
        gi_path = base / ".gitignore"
        if gi_path.is_file():
            text = gi_path.read_text(encoding="utf-8")
            if _GITIGNORE_LINE in text.splitlines():
                content = text
            else:
                content = (text.rstrip("\n") + "\n\n" + gitignore_comment + "\n"
                           + _GITIGNORE_LINE + "\n")
        else:
            content = gitignore_comment + "\n" + _GITIGNORE_LINE + "\n"
        action = _write_if_changed(gi_path, content)
        results.append({"path": display(".gitignore"), "action": action})

    return results


def _print_install_results(results: list) -> None:
    for entry in results:
        if entry["action"] == "written":
            print(f"-> {entry['path']}")
        else:
            print(f"ok {entry['path']} (up to date)")


def _diagnose_init(argv):
    parser = argparse.ArgumentParser(prog="tryaii diagnose init", add_help=False)
    parser.add_argument("--dir", default=".", dest="dir")
    parser.add_argument("--no-gitignore", action="store_true", dest="no_gitignore")
    args = parser.parse_args(argv)

    data = json.loads(_diagnose_data_path("skill.json").read_text(encoding="utf-8"))
    _print_install_results(_install_skill_files(
        Path(args.dir), args.dir.replace("\\", "/"), data, "tryaii-diagnose",
        _AGENTS_BEGIN, _AGENTS_END, "# tryaii diagnose runs (local)",
        args.no_gitignore))


def _diagnose_report(argv):
    parser = argparse.ArgumentParser(prog="tryaii diagnose report", add_help=False)
    parser.add_argument("--run")
    parser.add_argument("--out-dir", default=".tryaii/diagnose", dest="out_dir")
    parser.add_argument("--out")
    args = parser.parse_args(argv)

    from tryaii.diagnose import (
        latest_run_id,
        load_run_findings,
        previous_run_id,
        render_report_html,
    )

    out_dir = Path(args.out_dir)
    run_id = args.run or latest_run_id(out_dir)
    if run_id is None:
        print(f"error: no diagnose runs found in '{args.out_dir}' "
              "(run 'tryaii diagnose check' first)", file=sys.stderr)
        sys.exit(1)
    try:
        findings = load_run_findings(out_dir, run_id)
    except FileNotFoundError:
        print(f"error: run '{run_id}' not found in '{args.out_dir}'",
              file=sys.stderr)
        sys.exit(1)
    prev_id = previous_run_id(out_dir, run_id)
    previous = load_run_findings(out_dir, prev_id) if prev_id is not None else None

    html = render_report_html(findings, previous)
    out_dir_display = args.out_dir.replace("\\", "/")
    if args.out:
        out_path = Path(args.out)
        display = args.out.replace("\\", "/")
    else:
        out_path = out_dir / run_id / "index.html"
        display = f"{out_dir_display}/{run_id}/index.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(html)
    print(f"-> {display}")


# ---------------------------------------------------------------------------
# designpartner (see shared/designpartner/SPEC.md)
# ---------------------------------------------------------------------------

_DP_AGENTS_BEGIN = "<!-- tryaii-designpartner:begin -->"
_DP_AGENTS_END = "<!-- tryaii-designpartner:end -->"


def _designpartner_render(report: dict) -> str:
    """Human rendering of the status report -- byte-identical across CLIs."""
    stage = report["stage"]
    action = report["action"]
    buf = f"tryaii designpartner -- stage: {stage}\n\n"

    atype = action["type"]
    if atype == "enrolled":
        for entry in action.get("installed", []):
            if entry["action"] == "written":
                buf += f"-> {entry['path']}\n"
            else:
                buf += f"ok {entry['path']} (up to date)\n"
        buf += "\n"
    elif atype == "answers_saved":
        buf += f"answers saved ({action['count']})\n"
        for warning in action["warnings"]:
            buf += f"  dropped: {warning['question']} (not applicable)\n"
        buf += "\n"
    elif atype == "answers_rejected":
        buf += f"answers rejected: {len(action['problems'])} problem(s)\n"
        for problem in action["problems"]:
            buf += f"  - {problem['question']}: {problem['message']}\n"
        buf += "\n"
    elif atype == "consent_chosen":
        buf += f"consent recorded: {action['tier']}\n\n"
    elif atype == "submitted":
        submission = report["submission"]
        if submission["delivered"]:
            buf += f"delivered to {submission['url']}\n\n"
        else:
            buf += (f"could not reach {submission['url']} — submission saved "
                    f"locally at {submission['path']}\n\n")
    elif atype == "reset":
        buf += "enrollment state cleared\n"
        for removed in action["removed"]:
            buf += f"  removed {removed}\n"
        buf += "\n"

    if stage == "questionnaire" and "questionnaire" in report:
        questionnaire = report["questionnaire"]
        applicable = set(questionnaire["applicable"])
        answers = questionnaire["answers"]
        for section in questionnaire["sections"]:
            ids = [q["id"] for q in section["questions"] if q["id"] in applicable]
            answered = sum(1 for qid in ids if qid in answers)
            buf += f"  {section['title']}: {answered}/{len(ids)} answered\n"
        buf += "machine-readable catalog: tryaii designpartner --json\n"
    elif stage in ("consent", "diagnose"):
        consent = report["consent"]
        if stage == "diagnose":
            buf += (f"a diagnose run is required for tier '{consent['chosen']}' "
                    "— run the tryaii-diagnose skill or 'tryaii diagnose "
                    "check', then re-run designpartner\n")
        else:
            for tier in consent["tiers"]:
                buf += f"  {tier['id']} — {tier['title']}\n"
                buf += f"    {tier['copy']}\n"
    elif stage == "confirm":
        preview = report["preview"]
        buf += f"tier: {preview['tier']}\n"
        buf += "will send:\n"
        for item in preview["includes"]:
            buf += f"  - {item}\n"
        buf += f"-> {preview['path']}\n"
    elif stage == "submitted" and atype == "status":
        submission = report["submission"]
        if submission["delivered"]:
            buf += f"delivered to {submission['url']} at {submission['submitted_at']}\n"
        else:
            buf += f"saved locally at {submission['path']} (not delivered)\n"

    buf += f"\nnext: {report['next']['description']}\n"
    if report["next"]["command"] is not None:
        buf += f"  {report['next']['command']}\n"
    return buf


def cmd_designpartner(args):
    """The resumable design-partner command (shared/designpartner/SPEC.md)."""
    from tryaii import __version__
    from tryaii.designpartner import advance
    from tryaii.designpartner.state import STATE_FILE

    action_flags = [args.answers is not None, args.consent is not None,
                    args.confirm, args.reset]
    if sum(action_flags) > 1:
        print("error: pass at most one of --answers, --consent, --confirm, "
              "--reset", file=sys.stderr)
        sys.exit(2)

    answers = None
    if args.answers is not None:
        if args.answers == "-":
            raw = sys.stdin.read()
        else:
            path = Path(args.answers)
            if not path.is_file():
                raise FileNotFoundError(f"file not found: {args.answers}")
            raw = path.read_text(encoding="utf-8-sig")
        try:
            answers = json.loads(raw)
        except json.JSONDecodeError:
            where = "on stdin" if args.answers == "-" else f"in '{args.answers}'"
            print(f"error: invalid JSON {where}", file=sys.stderr)
            sys.exit(2)
        if not isinstance(answers, dict):
            print("error: answers must be a JSON object of "
                  '{"question_id": answer}', file=sys.stderr)
            sys.exit(2)

    # First run installs the agent playbook (skill + AGENTS.md block +
    # gitignore) before the engine ever runs.
    installed = None
    if not (Path(args.out_dir) / STATE_FILE).is_file() and not args.reset:
        data = json.loads(
            (Path(_designpartner_data_path("skill.json"))).read_text(encoding="utf-8"))
        installed = _install_skill_files(
            Path("."), ".", data, "tryaii-designpartner",
            _DP_AGENTS_BEGIN, _DP_AGENTS_END,
            "# tryaii designpartner state (local)", args.no_gitignore)

    now = args.now or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    stamp = args.stamp or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())

    try:
        report = advance(
            args.out_dir,
            {"answers": answers, "consent": args.consent,
             "confirm": args.confirm, "reset": args.reset},
            {"now": now, "stamp": stamp, "version": __version__, "url": None},
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)

    if installed is not None:
        report["action"]["installed"] = installed

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 -- best effort; never break output
        pass
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        _write_paced(_designpartner_render(report))


def _designpartner_data_path(name: str) -> Path:
    from tryaii import designpartner as dp_pkg

    return Path(dp_pkg.__file__).parent / "data" / name


def cmd_diagnose(argv):
    """Verb dispatcher for `tryaii diagnose` (verb-peeling; no argparse
    sub-subparsers exist in this CLI -- mirrors cmdDiagnose in cli.ts)."""
    verbs = {"init": _diagnose_init, "plan": _diagnose_plan,
             "check": _diagnose_check, "report": _diagnose_report}
    verb = argv[0] if argv else None
    if verb is None:
        print('error: missing diagnose verb. Run "tryaii help diagnose".',
              file=sys.stderr)
        sys.exit(2)
    handler = verbs.get(verb)
    if handler is None:
        print(f'error: unknown diagnose verb: {verb}. Run "tryaii help diagnose".',
              file=sys.stderr)
        sys.exit(2)
    try:
        handler(argv[1:])
    except SystemExit:
        raise
    except Exception as exc:
        # Same clean one-line contract as the handlers dispatch below.
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


def cli():
    """Main CLI entry point."""
    # -v/--verbose, --no-banner, -V/--version and -h/--help are handled before
    # argparse runs (see below) so they work in any position, matching the Node
    # CLI; they are intentionally not registered here.
    parser = argparse.ArgumentParser(
        prog="tryaii",
        description="TryAii -- Embedding-based AI model router",
    )

    subparsers = parser.add_subparsers(dest="command")

    # route
    route_parser = subparsers.add_parser("route", help="Route a prompt to the best model")
    route_parser.add_argument("prompt", help="The prompt to route")
    route_parser.add_argument("--quality", type=int, default=3, help="Quality priority (1-5)")
    route_parser.add_argument("--cost", type=int, default=3, help="Cost priority (1-5)")
    route_parser.add_argument("--speed", type=int, default=3, help="Speed priority (1-5)")
    route_parser.add_argument("--top-k", type=int, default=5, help="Number of recommendations")
    route_parser.add_argument(
        "--no-daemon", action="store_true", help="Route in-process; do not use or start a daemon"
    )

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
    eval_parser.add_argument(
        "--no-daemon", action="store_true", help="Route in-process; do not use or start a daemon"
    )

    # cachelint
    cachelint_parser = subparsers.add_parser(
        "cachelint", help="Pre-flight prompt-cache analysis")
    cachelint_parser.add_argument("input", help="Request JSON file, or '-' for stdin")
    cachelint_parser.add_argument(
        "--provider", help="Raw-text mode: provider for the raw prompt input")
    cachelint_parser.add_argument(
        "--model", help="Raw-text mode: model name (requires --provider)")
    cachelint_parser.add_argument(
        "--json", action="store_true", help="Emit the machine-readable result")

    # designpartner (single resumable command; no verbs)
    dp_parser = subparsers.add_parser(
        "designpartner", help="Enroll as a tryaii design partner")
    dp_parser.add_argument("--answers", help="Answers JSON file, or '-' for stdin")
    dp_parser.add_argument("--consent", help="Consent tier id")
    dp_parser.add_argument("--confirm", action="store_true")
    dp_parser.add_argument("--reset", action="store_true")
    dp_parser.add_argument("--json", action="store_true")
    dp_parser.add_argument("--out-dir", default=".tryaii/designpartner",
                           dest="out_dir")
    dp_parser.add_argument("--no-gitignore", action="store_true",
                           dest="no_gitignore")
    dp_parser.add_argument("--now")
    dp_parser.add_argument("--stamp")

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
    # subcommand (e.g. `tryaii eval --help`). All four git-style paths work:
    #   tryaii help            -> global overview
    #   tryaii help <command>  -> that command's detailed help
    #   tryaii <command> -h/--help -> that command's detailed help
    #   tryaii (bare)          -> global overview
    command = filtered[0] if filtered else None
    wants_help = "-h" in filtered or "--help" in filtered

    if command == "help":
        # First non-flag token is the topic. With no topic, bare `tryaii help`
        # prints the global overview, but `tryaii help -h/--help` documents the
        # help command itself -- consistent with `tryaii <command> --help`.
        topics = [a for a in filtered[1:] if not a.startswith("-")]
        topic = topics[0] if topics else None
        if topic is None:
            _write_paced(COMMAND_HELP["help"] if wants_help else HELP)
            return
        topic_help = COMMAND_HELP.get(topic)
        if topic_help is None:
            print(
                f'error: unknown help topic: {topic}. '
                'Run "tryaii help" for the list of commands.',
                file=sys.stderr,
            )
            sys.exit(2)
        _write_paced(topic_help)
        return

    if command is None:
        _write_paced(HELP)
        return

    if wants_help:
        if command == "diagnose":
            # `tryaii diagnose <verb> --help` gets the verb page.
            verbs = [a for a in filtered[1:] if not a.startswith("-")]
            verb_help = DIAGNOSE_VERB_HELP.get(verbs[0]) if verbs else None
            _write_paced(verb_help or COMMAND_HELP["diagnose"])
            return
        # Unknown command + --help still gets the global overview.
        _write_paced(COMMAND_HELP.get(command, HELP))
        return

    if command == "diagnose":
        # Verb-peeling dispatch (like the `help <topic>` handling above);
        # diagnose is not registered with argparse at all.
        cmd_diagnose(filtered[1:])
        return

    args = parser.parse_args(filtered)

    handlers = {
        "route": cmd_route,
        "eval": cmd_eval,
        "cachelint": cmd_cachelint,
        "designpartner": cmd_designpartner,
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
