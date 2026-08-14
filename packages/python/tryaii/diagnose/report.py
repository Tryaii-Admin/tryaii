"""HTML report renderer (SPEC.md §4).

One shared template (data/report_template.json, packed from
shared/diagnose/report/template.html) + a two-construct substitution
language, implemented identically in report.ts. Every scope value is a
pre-formatted STRING — no numbers cross the render boundary, so formatting
parity lives in the helpers here (and their mirrors) alone. A missing
template key is an error in both languages: template and scope builder
cannot drift apart silently.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

_TEMPLATE_PATH = Path(__file__).parent / "data" / "report_template.json"
_template_cache: Optional[str] = None

_SCALAR_RE = re.compile(r"\{\{([a-z_]+)\}\}")

CHECK_LABELS = {
    "model_fit": "model fit",
    "cache_readiness": "cache readiness",
    "cost_exposure": "cost exposure",
    "hygiene": "hygiene",
}
_BADGES = {"ok": "ok", "finding": "finding",
           "insufficient_data": "insufficient", "skipped": "skipped"}


def _load_template() -> str:
    global _template_cache
    if _template_cache is None:
        _template_cache = json.loads(
            _TEMPLATE_PATH.read_text(encoding="utf-8"))["html"]
    return _template_cache


# ---------------------------------------------------------------------------
# Substitution engine (SPEC §4.1)
# ---------------------------------------------------------------------------

def _esc(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))


def _lookup(chain: list, key: str):
    for scope in chain:
        if key in scope:
            return scope[key]
    raise KeyError(f"template key '{key}' missing from scope")


def _sub_scalars(text: str, chain: list) -> str:
    def repl(match):
        value = _lookup(chain, match.group(1))
        if not isinstance(value, str):
            raise TypeError(f"template key '{match.group(1)}' is not a string")
        return _esc(value)
    return _SCALAR_RE.sub(repl, text)


def render_template(template: str, scope: dict) -> str:
    return _render(template, [scope])


def _render(template: str, chain: list) -> str:
    out: list[str] = []
    pos = 0
    while True:
        begin = template.find("<!--BEGIN ", pos)
        if begin == -1:
            out.append(_sub_scalars(template[pos:], chain))
            break
        out.append(_sub_scalars(template[pos:begin], chain))
        name_end = template.find("-->", begin)
        name = template[begin + 10:name_end]
        end_marker = f"<!--END {name}-->"
        end = template.find(end_marker, name_end)
        if end == -1:
            raise ValueError(f"template block '{name}' has no END marker")
        inner = template[name_end + 3:end]
        items = _lookup(chain, name)
        if not isinstance(items, list):
            raise TypeError(f"template block '{name}' is not a list")
        for item in items:
            out.append(_render(inner, [item, *chain]))
        pos = end + len(end_marker)
    return "".join(out)


# ---------------------------------------------------------------------------
# Formatting helpers (SPEC §4.1 — mirrored in report.ts)
# ---------------------------------------------------------------------------

def _money2(x) -> str:
    return f"${x:.2f}"


def _money4(x) -> str:
    return f"${x:.4f}"


def _signed_money2(x) -> str:
    sign = "-" if x < 0 else "+"
    return f"{sign}${abs(x):.2f}"


def _s(value) -> str:
    """Stringify a §1.3-normalized number (or None -> 'n/a')."""
    return "n/a" if value is None else str(value)


# ---------------------------------------------------------------------------
# Scope builder
# ---------------------------------------------------------------------------

def _delta_scope(findings: dict, previous: dict) -> dict:
    """SPEC §4.2 — per-site check-status transitions vs the previous run."""
    prev_sites = {site["site_id"]: site for site in previous["sites"]}
    cur_sites = {site["site_id"]: site for site in findings["sites"]}

    improved = regressed = 0
    for site_id, cur in cur_sites.items():
        prev = prev_sites.get(site_id)
        if prev is None:
            continue
        ups = downs = 0
        for check in CHECK_LABELS:
            a = prev["checks"][check]["status"]
            b = cur["checks"][check]["status"]
            if a in ("ok", "finding") and b in ("ok", "finding"):
                if a == "finding" and b == "ok":
                    ups += 1
                elif a == "ok" and b == "finding":
                    downs += 1
        if downs:
            regressed += 1
        elif ups:
            improved += 1

    added = sum(1 for site_id in cur_sites if site_id not in prev_sites)
    removed = sum(1 for site_id in prev_sites if site_id not in cur_sites)

    monthly: list[dict] = []
    cur_total = findings["summary"]["totals"]["est_monthly_cost_usd"]
    prev_total = previous["summary"]["totals"]["est_monthly_cost_usd"]
    if cur_total is not None and prev_total is not None:
        monthly.append({"value": _signed_money2(cur_total - prev_total)})

    return {
        "prev_run_id": previous["run_id"],
        "improved": str(improved),
        "regressed": str(regressed),
        "added": str(added),
        "removed": str(removed),
        "monthly": monthly,
    }


def _model_fit_scope(payload: dict) -> list[dict]:
    if "top" not in payload:
        return []
    cls = payload["classification"]
    broad = cls["broad_category"]
    sub = cls["subcategory"]
    category = f"{broad} > {sub}" if broad is not None and sub is not None else "n/a"
    current_rank = payload["current_rank"]

    rows = []
    for entry in payload["top"]:
        rows.append({
            "rank": str(entry["rank"]),
            "model_id": entry["model_id"],
            "cls": "current" if entry["rank"] == current_rank else "",
            "q": _s(entry["quality_score"]),
            "c": _s(entry["cost_score"]),
            "s": _s(entry["speed_score"]),
            "reasoning": entry["reasoning"],
        })
    # The user's model always appears, even when it ranks below the top 5.
    if current_rank is not None and current_rank > len(payload["top"]):
        current = payload["current"]
        rows.append({
            "rank": str(current_rank),
            "model_id": current["model_id"],
            "cls": "current",
            "q": _s(current["quality_score"]),
            "c": _s(current["cost_score"]),
            "s": _s(current["speed_score"]),
            "reasoning": "",
        })

    return [{
        "summary": payload["summary"],
        "category": category,
        "confidence": _s(cls["confidence"]),
        "rows": rows,
    }]


def _cache_scope(payload: dict) -> list[dict]:
    if "verdict" not in payload:
        return []
    prefix = payload["stable_prefix"]
    findings_items = [
        {
            "kind": f["kind"],
            "where": f"{f['section']}+{f['section_offset']}",
            "excerpt": f["excerpt"],
            "why": f["why"],
        }
        for f in payload["top_findings"]
    ]
    recs_items = [{"text": text} for text in payload["recommendations"]]
    return [{
        "verdict_code": payload["verdict"]["code"],
        "verdict_summary": payload["verdict"]["summary"],
        "total_tokens": _s(payload["total_tokens"]),
        "threshold": _s(payload["threshold_min_tokens"]),
        "stable_tokens": _s(prefix["tokens"]),
        "stable_pct": _s(prefix["pct_of_prompt"]),
        "bar_width": _s(prefix["pct_of_prompt"]),
        "findings": [{"items": findings_items}] if findings_items else [],
        "recs": [{"items": recs_items}] if recs_items else [],
    }]


def _cost_scope(payload: dict) -> list[dict]:
    if payload["status"] not in ("ok", "finding"):
        return []
    rows = [
        {"label": "input tokens",
         "value": f"{payload['input_tokens']} ({payload['token_method']})"},
        {"label": "output tokens", "value": _s(payload["output_tokens"])},
        {"label": "cost per call", "value": _money4(payload["cost_per_call_usd"])},
    ]
    monthly = payload["monthly"]
    if monthly["status"] == "ok":
        rows.append({
            "label": f"monthly cost ({_s(monthly['calls_per_day'])} calls/day)",
            "value": _money2(monthly["cost_usd"]),
        })
        if "cache_savings_usd" in monthly:
            rows.append({"label": "cache savings (max)",
                         "value": _money2(monthly["cache_savings_usd"])})
    else:
        rows.append({"label": "monthly",
                     "value": f"insufficient data: {monthly['reason']}"})
    swap = payload["swap"]
    if swap is not None:
        value = f"{swap['model_id']} at {_money4(swap['cost_per_call_usd'])}/call"
        if "monthly_savings_usd" in swap:
            value += f" (saves {_money2(swap['monthly_savings_usd'])}/mo)"
        rows.append({"label": "cheaper swap", "value": value})
    else:
        rows.append({"label": "cheaper swap",
                     "value": "none within quality tolerance"})
    return [{"rows": rows}]


def _hygiene_scope(payload: dict) -> list[dict]:
    if payload["status"] not in ("ok", "finding"):
        return []
    findings_items = [
        {
            "kind": f["kind"],
            "where": f"{f['section']}+{f['section_offset']}",
            "excerpt": f["excerpt"],
            "hint": f["hint"],
        }
        for f in payload["findings"]
    ]
    advisory_items = [{"text": text} for text in payload["advisories"]]
    return [{
        "stats": (f"{payload['findings_count']} finding(s), "
                  f"{payload['blocking_count']} blocking, "
                  f"{payload['findings_in_system']} in system"),
        "findings": [{"items": findings_items}] if findings_items else [],
        "advisories": [{"items": advisory_items}] if advisory_items else [],
    }]


def _site_scope(site: dict) -> dict:
    checks = site["checks"]
    chips = []
    reasons = []
    for check, label in CHECK_LABELS.items():
        payload = checks[check]
        chips.append({"cls": _BADGES[payload["status"]], "label": label,
                      "badge": _BADGES[payload["status"]]})
        if payload["status"] == "insufficient_data":
            reasons.append({"label": label, "text": payload["reason"]})
    return {
        "site_id": site["site_id"],
        "line": str(site["line"]),
        "model_label": site["model"] if site["model"] is not None else "none declared",
        "chips": chips,
        "reasons": reasons,
        "mf": _model_fit_scope(checks["model_fit"]),
        "cache": _cache_scope(checks["cache_readiness"]),
        "cost": _cost_scope(checks["cost_exposure"]),
        "hyg": _hygiene_scope(checks["hygiene"]),
    }


def build_scope(findings: dict, previous: Optional[dict] = None) -> dict:
    summary = findings["summary"]
    totals = summary["totals"]

    findings_total = sum(
        counts["finding"] for counts in summary["check_status_counts"].values())

    skipped_items = findings["inventory"]["skipped"]
    skipped: list[dict] = []
    if skipped_items:
        skipped.append({
            "count": str(len(skipped_items)),
            "items": [{"index": str(item["index"]), "reason": item["reason"]}
                      for item in skipped_items],
        })

    status_rows = [
        {"check": check, "ok": str(counts["ok"]), "finding": str(counts["finding"]),
         "insufficient": str(counts["insufficient_data"]),
         "skipped": str(counts["skipped"])}
        for check, counts in summary["check_status_counts"].items()
    ]

    files: list[dict] = []
    by_file: dict[str, dict] = {}
    for site in findings["sites"]:
        group = by_file.get(site["file"])
        if group is None:
            group = {"file": site["file"], "sites": []}
            by_file[site["file"]] = group
            files.append(group)
        group["sites"].append(_site_scope(site))

    goal = findings["interview"]["goal"]
    priorities = findings["interview"]["priorities"]

    def _tile(value: Any) -> str:
        return "n/a" if value is None else _money2(value)

    return {
        "site_count": str(summary["site_count"]),
        "run_id": findings["run_id"],
        "generated_at": _s(findings["generated_at"]),
        "tool_version": findings["tool"]["version"],
        "q": str(priorities["quality"]),
        "c": str(priorities["cost"]),
        "s": str(priorities["speed"]),
        "findings_total": str(findings_total),
        "findings_cls": " warn" if findings_total > 0 else "",
        "est_cost": _tile(totals["est_monthly_cost_usd"]),
        "cache_savings": _tile(totals["est_monthly_cache_savings_usd"]),
        "cache_savings_cls": (
            " ok" if totals["est_monthly_cache_savings_usd"] is not None else ""),
        "swap_savings": _tile(totals["est_monthly_swap_savings_usd"]),
        "swap_savings_cls": (
            " ok" if totals["est_monthly_swap_savings_usd"] is not None else ""),
        "goal": [{"text": goal}] if goal is not None else [],
        "delta": [_delta_scope(findings, previous)] if previous is not None else [],
        "skipped": skipped,
        "status_rows": status_rows,
        "files": files,
    }


def render_report_html(findings: dict, previous: Optional[dict] = None) -> str:
    """findings (+ optional previous run's findings) -> self-contained HTML."""
    return render_template(_load_template(), build_scope(findings, previous))
