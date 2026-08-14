"""Orchestrator: inventory -> findings document (SPEC.md §2–§3).

Deterministic and clock-free: run_id / generated_at / version arrive via
`opts`, classification arrives via each site's `_classification` seam or the
injected `classify_fn` — the engine itself never touches a model, the
network, or a clock.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

from tryaii.cachelint._jsonutil import normalize_numbers
from tryaii.cachelint.analyzer import analyze_item, build_canonical
from tryaii.diagnose.cost import run_cost
from tryaii.diagnose.hygiene import run_hygiene
from tryaii.diagnose.intake import _clean_classification, normalize_inventory
from tryaii.diagnose.modelfit import run_model_fit
from tryaii.diagnose.resolve import resolve_model_id
from tryaii.registry.models import ModelRegistry
from tryaii.scoring.priorities import Priorities

DEFAULT_CHECKS = ("model_fit", "cache_readiness", "cost_exposure", "hygiene")

CACHE_TOP_FINDINGS = 3

_COSTMODEL_PATH = Path(__file__).parent / "data" / "costmodel.json"
_costmodel_cache: Optional[dict] = None


def _read_discount_factors() -> dict:
    global _costmodel_cache
    if _costmodel_cache is None:
        _costmodel_cache = json.loads(_COSTMODEL_PATH.read_text(encoding="utf-8"))
    return _costmodel_cache["read_discount_factors"]


def _insufficient(reason: str) -> dict:
    return {"status": "insufficient_data", "reason": reason}


_SKIPPED = {"status": "skipped", "reason": "not selected"}


def _select_checks(requested: Optional[list]) -> list[str]:
    if requested is None:
        return list(DEFAULT_CHECKS)
    unknown = [c for c in requested if c not in DEFAULT_CHECKS]
    if unknown:
        raise ValueError(f"unknown check '{unknown[0]}'")
    selected = [c for c in DEFAULT_CHECKS if c in requested]
    if not selected:
        raise ValueError("no checks selected")
    return selected


def _run_cache_readiness(prompt: Any, provider: Optional[str],
                         model: Optional[str]) -> tuple[dict, Optional[dict]]:
    """Returns (payload, cache_ctx) — cache_ctx feeds the cost check and is
    never emitted."""
    if prompt is None:
        return _insufficient("no prompt"), None
    if provider is None:
        return _insufficient("no provider declared"), None
    try:
        item = analyze_item({"prompt": prompt,
                             "llm": {"provider": provider, "name": model or ""}})
    except ValueError as exc:
        return _insufficient(str(exc)), None
    data = item.data
    verdict_code = data["verdict"]["code"]
    payload = {
        "status": "ok" if verdict_code == "CACHEABLE" else "finding",
        "reason": None,
        "verdict": data["verdict"],
        "threshold_min_tokens": data["threshold"]["min_tokens"],
        "total_tokens": data["token_report"]["total"]["tokens"],
        "stable_prefix": {
            "tokens": data["stable_prefix"]["tokens"],
            "pct_of_prompt": data["stable_prefix"]["pct_of_prompt"],
            "if_first_fixed_tokens": data["stable_prefix"]["if_first_fixed_tokens"],
        },
        "findings_count": len(data["findings"]),
        "top_findings": data["findings"][:CACHE_TOP_FINDINGS],
        "recommendations": data["recommendations"],
    }
    cache_ctx = {
        "verdict_code": verdict_code,
        "stable_tokens": data["stable_prefix"]["tokens"],
        "total_tokens": data["token_report"]["total"]["tokens"],
        "provider_key": data["provider_key"],
        "upstream": data["upstream"],
    }
    return payload, cache_ctx


def analyze_inventory(
    data: Any,
    opts: Optional[dict] = None,
    classify_fn: Optional[Callable[[str], Optional[dict]]] = None,
    registry: Optional[ModelRegistry] = None,
) -> dict:
    """Run the selected checks over an inventory; returns the findings
    document (SPEC.md §3). Raises ValueError on an unusable inventory or an
    unknown check name; everything site-level degrades per SPEC.md §2.0."""
    opts = opts or {}
    norm = normalize_inventory(data)
    priorities = Priorities.from_dict(opts.get("priorities") or {})
    checks = _select_checks(opts.get("checks"))
    registry = registry or ModelRegistry.default()
    factors = _read_discount_factors()

    default_calls = (opts["calls_per_day"] if opts.get("calls_per_day") is not None
                     else norm["defaults"]["calls_per_day"])
    default_output = (opts["output_tokens"] if opts.get("output_tokens") is not None
                      else norm["defaults"]["output_tokens"])
    goal = opts.get("goal") or None

    sites_out: list[dict] = []
    for site in norm["sites"]:
        prompt = site["prompt"]
        canonical = build_canonical(prompt)[0] if prompt is not None else None

        resolved_id, _method = resolve_model_id(site["model"], registry)

        classification = site["classification"]
        if classification is None and classify_fn is not None and canonical is not None:
            classification = _clean_classification(classify_fn(canonical))

        # model_fit
        fit_internal: Optional[dict] = None
        if "model_fit" not in checks:
            fit_payload = dict(_SKIPPED)
        elif prompt is None:
            fit_payload = _insufficient("no prompt")
        elif classification is None:
            fit_payload = _insufficient("no classifier available")
        else:
            fit_payload, fit_internal = run_model_fit(
                classification, resolved_id, site["model"], priorities, registry)

        # cache_readiness
        cache_ctx: Optional[dict] = None
        if "cache_readiness" not in checks:
            cache_payload = dict(_SKIPPED)
        else:
            cache_payload, cache_ctx = _run_cache_readiness(
                prompt, site["provider"], site["model"])

        # cost_exposure
        if "cost_exposure" not in checks:
            cost_payload = dict(_SKIPPED)
        else:
            cost_payload = run_cost(
                canonical_text=canonical,
                resolved_model_id=resolved_id,
                declared_model=site["model"],
                registry=registry,
                output_tokens=site["output_tokens"] or default_output,
                calls_per_day=(site["calls_per_day"]
                               if site["calls_per_day"] is not None
                               else default_calls),
                cache_ctx=cache_ctx,
                fit_internal=fit_internal,
                read_discount_factors=factors,
            )

        # hygiene
        if "hygiene" not in checks:
            hygiene_payload = dict(_SKIPPED)
        elif prompt is None:
            hygiene_payload = _insufficient("no prompt")
        else:
            hygiene_payload = run_hygiene(prompt)

        entry = {
            "site_id": site["site_id"],
            "file": site["file"],
            "line": site["line"],
            "provider": site["provider"],
            "model": site["model"],
            "resolved_model_id": resolved_id,
        }
        if site["notes"] is not None:
            entry["notes"] = site["notes"]
        entry["checks"] = {
            "model_fit": fit_payload,
            "cache_readiness": cache_payload,
            "cost_exposure": cost_payload,
            "hygiene": hygiene_payload,
        }
        sites_out.append(entry)

    run_id = opts.get("run_id") or "run"
    generated_at = opts.get("now")
    interview = {
        "priorities": priorities.to_dict(),
        "goal": goal,
        "defaults": {"calls_per_day": default_calls,
                     "output_tokens": default_output},
    }

    doc = {
        "version": 1,
        "run_id": run_id,
        "generated_at": generated_at,
        "tool": {"name": "tryaii", "version": opts.get("version") or "0.0.0"},
        "interview": interview,
        "inventory": {"site_count": len(sites_out), "skipped": norm["skipped"]},
        "sites": sites_out,
        "summary": _summary(run_id, generated_at, goal, priorities, sites_out),
    }
    return normalize_numbers(doc)


def _summary(run_id: str, generated_at: Optional[str], goal: Optional[str],
             priorities: Priorities, sites: list[dict]) -> dict:
    """The redacted layer (SPEC.md §3.1) — no code, prompts, paths, reasoning
    text, or goal text ever lands here."""
    status_counts = {
        check: {"ok": 0, "finding": 0, "insufficient_data": 0, "skipped": 0}
        for check in DEFAULT_CHECKS
    }
    verdict_counts: dict[str, int] = {}
    monthly_costs: list[float] = []
    cache_savings: list[float] = []
    swap_savings: list[float] = []
    traffic_sites = 0
    swap_sites = 0
    ranks: list[int] = []

    for site in sites:
        for check, payload in site["checks"].items():
            status_counts[check][payload["status"]] += 1

        cache = site["checks"]["cache_readiness"]
        if cache["status"] in ("ok", "finding"):
            code = cache["verdict"]["code"]
            verdict_counts[code] = verdict_counts.get(code, 0) + 1

        cost = site["checks"]["cost_exposure"]
        monthly = cost.get("monthly")
        if isinstance(monthly, dict) and monthly.get("status") == "ok":
            traffic_sites += 1
            monthly_costs.append(monthly["cost_usd"])
            if "cache_savings_usd" in monthly:
                cache_savings.append(monthly["cache_savings_usd"])
        swap = cost.get("swap")
        if swap is not None:
            swap_sites += 1
            if "monthly_savings_usd" in swap:
                swap_savings.append(swap["monthly_savings_usd"])

        fit = site["checks"]["model_fit"]
        if fit.get("current_rank") is not None:
            ranks.append(fit["current_rank"])

    def _total(values: list[float]) -> Optional[float]:
        return round(sum(values), 4) if values else None

    def _median(values: list[int]) -> Optional[float]:
        if not values:
            return None
        s = sorted(values)
        mid = len(s) // 2
        return float(s[mid]) if len(s) % 2 == 1 else (s[mid - 1] + s[mid]) / 2

    return {
        "schema": "tryaii.diagnose.summary/1",
        "run_id": run_id,
        "generated_at": generated_at,
        "site_count": len(sites),
        "goal_present": goal is not None,
        "priorities": priorities.to_dict(),
        "check_status_counts": status_counts,
        "cache_verdict_counts": verdict_counts,
        "totals": {
            "est_monthly_cost_usd": _total(monthly_costs),
            "est_monthly_cache_savings_usd": _total(cache_savings),
            "est_monthly_swap_savings_usd": _total(swap_savings),
            "sites_with_traffic_data": traffic_sites,
        },
        "swap_stats": {
            "sites_with_cheaper_swap": swap_sites,
            "median_current_rank": _median(ranks),
        },
    }
