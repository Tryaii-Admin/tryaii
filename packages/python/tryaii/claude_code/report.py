"""`tryaii claude-code report`: the usage summary (document + text rendering).

The document (``tryaii.claude-code.report/1``) holds counts, token sums and
cost estimates only: model ids, process names (``main``, the subagent type),
project labels (base names) and dates (``days``: one row per UTC date). Never
message text, and never a session id: cold cache restarts (``sessions``) name a
session only by its ordinal ("Session 3", numbered by first call in the
window). When
``tryaii claude-code analyze`` has run, the CLI adds its stored result (no
text either) as ``analysis`` and the text output gets a section for it.
"""

from __future__ import annotations

import math
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from tryaii.claude_code import sessions as sess
from tryaii.claude_code.pricing import COST_PARTS, Pricer, cheapest_tier_model
from tryaii.monitor.events import format_ts

REPORT_SCHEMA = "tryaii.claude-code.report/1"
COST_LABEL = "estimate at API list prices"
TOP_PROJECTS = 10
#: A subagent type is a candidate for a cheaper model when the median of its
#: runs' total output is below this many tokens (short jobs: search, lookups,
#: triage). A run is one subagent invocation: all calls of one subagent file
#: (or one ``agentId``).
SMALL_RUN_OUTPUT_TOKENS = 2000
#: ... and its p90 run stays below this (a type with some long runs is not small).
SMALL_RUN_P90_OUTPUT_TOKENS = 8000
MIN_SUGGESTION_RUNS = 3
MAX_SUGGESTIONS = 5
CHEAP_FAMILY = "haiku"
TOP_COLD_SESSIONS = 10
TOP_COLD_RESTARTS = 5
COLD_RULE = ("main thread of one session; more than 5 minutes since its previous call and a "
             "cache write of at least max(1024, cache read) tokens; cost = the write above "
             "reading the same tokens, at API list prices")


def _r(value: Optional[float], nd: int = 6) -> Optional[float]:
    return None if value is None else round(value, nd)


def _token_sums(calls: list) -> dict:
    return {
        "input": sum(c.tokens("input_tokens") for c in calls),
        "cache_write": sum(c.cache_write_tokens for c in calls),
        "cache_write_5m": sum(max(c.cache_write_tokens - c.tokens("cache_write_1h_tokens"), 0)
                              for c in calls),
        "cache_write_1h": sum(c.tokens("cache_write_1h_tokens") for c in calls),
        "cache_read": sum(c.tokens("cache_read_input_tokens") for c in calls),
        "output": sum(c.tokens("output_tokens") for c in calls),
        "thinking": sum(c.tokens("thinking_tokens") for c in calls),
    }


def _hit_share(tokens: dict) -> Optional[float]:
    context = tokens["input"] + tokens["cache_write"] + tokens["cache_read"]
    return round(tokens["cache_read"] / context, 4) if context else None


def _group(calls: list, pricer: Pricer, costs: dict) -> dict:
    tokens = _token_sums(calls)
    priced = [costs[c.key] for c in calls if costs.get(c.key) is not None]
    outputs = [c.tokens("output_tokens") for c in calls]
    split = {part: _r(sum(p[part] for p in priced)) for part in COST_PARTS}
    return {
        "calls": len(calls),
        "unpriced_calls": len(calls) - len(priced),
        "tokens": tokens,
        "cache_hit_share": _hit_share(tokens),
        "median_output_tokens": statistics.median(outputs) if outputs else None,
        "est_cost_usd": _r(sum(p["total"] for p in priced)) if priced else (0 if calls else None),
        "cost_split_usd": split,
    }


def _by(calls: list, key) -> dict:
    out: dict = {}
    for c in calls:
        out.setdefault(key(c), []).append(c)
    return out


def _utc_date(dt: datetime) -> date:
    return (dt.astimezone(timezone.utc) if dt.tzinfo is not None else dt).date()


def day_rows(calls: list, costs: dict, since: datetime, now: datetime) -> list:
    """One row per UTC calendar date from ``since`` to ``now`` (both edge days
    are partial), oldest first. A date with no calls is a row of zeros (cost 0,
    cache hit share None), so the dates are contiguous."""
    by_date = _by(calls, lambda c: _utc_date(c.ts))
    rows = []
    day, last = _utc_date(since), _utc_date(now)
    while day <= last:
        group = by_date.get(day, [])
        tokens = _token_sums(group)
        priced = [costs[c.key] for c in group if costs.get(c.key) is not None]
        subagent = sum(1 for c in group if c.subagent)
        rows.append({"date": day.isoformat(), "calls": len(group),
                     "unpriced_calls": len(group) - len(priced),
                     "main_calls": len(group) - subagent, "subagent_calls": subagent,
                     "cache_hit_share": _hit_share(tokens),
                     "est_cost_usd": _r(sum(p["total"] for p in priced)) if priced else 0})
        day += timedelta(days=1)
    return rows


def build_report(calls: list, pricer: Pricer, *, days: int, now: datetime,
                 since: datetime, files: int, projects_dir: str) -> dict:
    costs = {c.key: pricer.cost(c) for c in calls}
    total = _group(calls, pricer, costs)

    models = []
    for model, group in sorted(_by(calls, lambda c: c.model).items()):
        price = pricer.price(model)
        row = {"model": model, "catalog_id": price.catalog_id if price else None,
               "priced": price is not None,
               "price_source": price.source if price else "unpriced",
               "cache_read_price": price.cache_read_source if price else None}
        row.update(_group(group, pricer, costs))
        models.append(row)
    models.sort(key=lambda r: (-(r["est_cost_usd"] or 0), r["model"]))

    threads = []
    for name, pred in (("main", lambda c: not c.subagent), ("subagents", lambda c: c.subagent)):
        group = [c for c in calls if pred(c)]
        row = {"thread": name}
        row.update(_group(group, pricer, costs))
        threads.append(row)

    processes = []
    for name, group in _by(calls, lambda c: c.process).items():
        row = {"process": name}
        row.update(_group(group, pricer, costs))
        processes.append(row)
    processes.sort(key=lambda r: (-(r["est_cost_usd"] or 0), r["process"]))

    projects = []
    for name, group in _by(calls, lambda c: c.project).items():
        g = _group(group, pricer, costs)
        projects.append({"project": name, "calls": g["calls"], "est_cost_usd": g["est_cost_usd"],
                         "cache_hit_share": g["cache_hit_share"]})
    projects.sort(key=lambda r: (-(r["est_cost_usd"] or 0), r["project"]))

    unpriced = [{"model": r["model"], "calls": r["calls"],
                 "tokens": r["tokens"]["input"] + r["tokens"]["cache_write"]
                 + r["tokens"]["cache_read"] + r["tokens"]["output"]}
                for r in models if not r["priced"]]

    return {
        "schema": REPORT_SCHEMA,
        "cost_basis": COST_LABEL,
        "pricing_rule": "catalog input/output prices (list price fallback for Claude models "
                        "the catalog lacks); cache write 1.25x input (5m) and 2x input (1h); "
                        "cache read at the catalog price, else a per-model multiplier of "
                        "input (opus-5-5 0.05, fable-5-1 / mythos-5-1 0.025, else 0.1)",
        "catalog_version": pricer.catalog_version,
        "window": {"days": days, "start": format_ts(since), "end": format_ts(now)},
        "source": {"projects_dir": projects_dir, "files": files},
        "totals": total,
        "models": models,
        "threads": threads,
        "processes": processes,
        "top_projects": projects[:TOP_PROJECTS],
        "days": day_rows(calls, costs, since, now),
        "unpriced_models": unpriced,
        "subagent_runs": run_stats(calls, costs),
        "suggestions": suggestions(calls, pricer, costs),
        "cold_restarts": cold_restart_summary(calls, pricer),
    }


def cold_restart_summary(calls: list, pricer: Pricer) -> dict:
    """Cold cache restarts of the window's main-thread calls (``sessions``):
    count and cost at list price, by reason, per session (by ordinal only)
    and the largest few. Never a session id."""
    labels = sess.session_labels(calls)
    rows = []
    for c in calls:
        if c.cold is None:
            continue
        cost = sess.restart_cost(c, pricer.price(c.model))
        rows.append((c, cost))
    priced = [cost for _c, cost in rows if cost is not None]

    def total(items):
        values = [cost for _c, cost in items if cost is not None]
        return _r(sum(values)) if values else (0 if not items else None)

    by_reason = []
    for reason in sess.REASONS:
        items = [(c, cost) for c, cost in rows if c.cold.reason == reason]
        by_reason.append({"reason": reason, "count": len(items), "est_cost_usd": total(items)})
    per_session: dict = {}
    for c, cost in rows:
        per_session.setdefault(c.session, []).append((c, cost))
    session_rows = [{"session": labels.get(s, "Session ?"), "cold_restarts": len(items),
                     "est_cost_usd": total(items)} for s, items in per_session.items()]
    session_rows.sort(key=lambda r: (-(r["est_cost_usd"] or 0), r["session"]))
    largest = sorted(rows, key=lambda x: (-(x[1] or 0), x[0].ts))[:TOP_COLD_RESTARTS]
    return {
        "rule": COLD_RULE,
        "main_sessions": len(labels),
        "sessions_with_cold_restarts": len(per_session),
        "count": len(rows),
        "unpriced": len(rows) - len(priced),
        "est_cost_usd": _r(sum(priced)) if priced else (0 if not rows else None),
        "by_reason": by_reason,
        "sessions": session_rows[:TOP_COLD_SESSIONS],
        "largest": [{"ts": format_ts(c.ts), "session": labels.get(c.session, "Session ?"),
                     "model": c.model, "reason": c.cold.reason, "ttl": c.cold.ttl,
                     "gap_minutes": round(c.cold.gap_s / 60, 1),
                     "write_tokens": c.cold.write_tokens, "est_cost_usd": _r(cost)}
                    for c, cost in largest],
    }


def _p90(values: list):
    """Nearest-rank 90th percentile; None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(math.ceil(0.9 * len(ordered)) - 1, 0)]


def _median(values: list):
    return statistics.median(values) if values else None


def subagent_runs(calls: list, costs: dict) -> dict:
    """Subagent type -> its runs: ``{"calls", "output", "cost", "unpriced"}``
    per run (cost sums the priced calls)."""
    runs: dict = {}
    for c in calls:
        if not c.subagent:
            continue
        run = runs.setdefault(c.process, {}).setdefault(
            c.run or c.key, {"calls": [], "output": 0, "cost": 0.0, "unpriced": 0})
        run["calls"].append(c)
        run["output"] += c.tokens("output_tokens")
        cost = costs.get(c.key)
        if cost is None:
            run["unpriced"] += 1
        else:
            run["cost"] += cost["total"]
    return runs


def run_stats(calls: list, costs: dict) -> list:
    """Per subagent type: runs, calls, and the median / p90 across runs of the
    run's total output and total cost."""
    out = []
    for process, runs in subagent_runs(calls, costs).items():
        values = list(runs.values())
        outputs = [r["output"] for r in values]
        run_costs = [r["cost"] for r in values]
        out.append({"process": process, "runs": len(values),
                    "calls": sum(len(r["calls"]) for r in values),
                    "median_run_output_tokens": _median(outputs),
                    "p90_run_output_tokens": _p90(outputs),
                    "median_run_cost_usd": _r(_median(run_costs)),
                    "p90_run_cost_usd": _r(_p90(run_costs)),
                    "est_cost_usd": _r(sum(run_costs))})
    out.sort(key=lambda r: (-(r["est_cost_usd"] or 0), r["process"]))
    return out


def suggestions(calls: list, pricer: Pricer, costs: Optional[dict] = None) -> list:
    """Subagent types whose RUNS are small (median total output per run below
    ``SMALL_RUN_OUTPUT_TOKENS`` and p90 below ``SMALL_RUN_P90_OUTPUT_TOKENS``,
    at least ``MIN_SUGGESTION_RUNS`` runs): the
    same usage of their non-Haiku, priced calls priced at the newest Haiku is
    the UPPER BOUND of what ``model: haiku`` in that subagent's definition
    could save (same tokens, and only if Haiku does the task as well)."""
    target = cheapest_tier_model(pricer, CHEAP_FAMILY)
    if target is None:
        return []
    costs = costs if costs is not None else {c.key: pricer.cost(c) for c in calls}
    stats = {r["process"]: r for r in run_stats(calls, costs)}
    out = []
    for process, runs in subagent_runs(calls, costs).items():
        row = stats[process]
        if row["runs"] < MIN_SUGGESTION_RUNS:
            continue
        if (row["median_run_output_tokens"] >= SMALL_RUN_OUTPUT_TOKENS
                or row["p90_run_output_tokens"] >= SMALL_RUN_P90_OUTPUT_TOKENS):
            continue
        movable = [c for r in runs.values() for c in r["calls"]
                   if CHEAP_FAMILY not in c.model and costs.get(c.key) is not None]
        if not movable:
            continue
        now_cost = sum(costs[c.key]["total"] for c in movable)
        saving = now_cost - sum(pricer.cost(c, target)["total"] for c in movable)
        if saving <= 0:
            continue
        out.append({"process": process, "runs": row["runs"], "calls": len(movable),
                    "models": sorted({c.model for c in movable}),
                    "median_run_output_tokens": row["median_run_output_tokens"],
                    "p90_run_output_tokens": row["p90_run_output_tokens"],
                    "median_run_cost_usd": row["median_run_cost_usd"],
                    "p90_run_cost_usd": row["p90_run_cost_usd"],
                    "est_cost_usd": _r(now_cost), "suggested_model": "haiku",
                    "priced_as": target, "max_saving_usd": _r(saving)})
    out.sort(key=lambda r: (-r["max_saving_usd"], r["process"]))
    return out[:MAX_SUGGESTIONS]


# --- text ----------------------------------------------------------------------

def _n(value) -> str:
    return "-" if value is None else f"{int(value):,}"


def _usd(value) -> str:
    return "-" if value is None else f"${value:,.2f}"


def _pct(value) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def _table(headers: tuple, rows: list, right: set) -> list:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = []
    for row in (headers, *rows):
        cells = [cell.rjust(widths[i]) if headers[i] in right and row is not headers
                 else cell.ljust(widths[i]) for i, cell in enumerate(row)]
        lines.append("  " + "  ".join(cells).rstrip())
    return lines


def render_report(doc: dict) -> str:
    w = doc["window"]
    t = doc["totals"]
    tok = t["tokens"]
    split = t["cost_split_usd"]
    lines = [
        f"Claude Code usage -- last {w['days']} days ({w['start'][:10]} .. {w['end'][:10]})",
        f"source: {doc['source']['projects_dir']} ({doc['source']['files']} log files)",
        f"costs: {COST_LABEL} (TryAii catalog {doc['catalog_version'] or '-'}); "
        "cache write 1.25x / 2x input (5m / 1h), cache read per model (column 'cache read')",
        "",
    ]
    if not t["calls"]:
        lines.append("no Claude Code calls in this window")
        return "\n".join(lines) + "\n"
    lines += [
        f"calls: {_n(t['calls'])}"
        + (f" ({_n(t['unpriced_calls'])} unpriced)" if t["unpriced_calls"] else ""),
        f"tokens: input {_n(tok['input'])}, cache write {_n(tok['cache_write'])} "
        f"(5m {_n(tok['cache_write_5m'])}, 1h {_n(tok['cache_write_1h'])}), "
        f"cache read {_n(tok['cache_read'])}, output {_n(tok['output'])} "
        f"(thinking {_n(tok['thinking'])})",
        f"cache hit share: {_pct(t['cache_hit_share'])} of input tokens read from cache",
        f"est cost: {_usd(t['est_cost_usd'])} = input {_usd(split['input'])} + cache write 5m "
        f"{_usd(split['cache_write_5m'])} + cache write 1h {_usd(split['cache_write_1h'])} + "
        f"cache read {_usd(split['cache_read'])} + output {_usd(split['output'])}",
        "",
        "By model",
    ]
    rows = [(r["model"], r["catalog_id"] or "-", r["price_source"], r["cache_read_price"] or "-",
             _n(r["calls"]), _pct(r["cache_hit_share"]), _n(r["tokens"]["output"]),
             _usd(r["est_cost_usd"]) if r["priced"] else "-")
            for r in doc["models"]]
    lines += _table(("model", "catalog id", "price", "cache read", "calls", "cache hit", "output",
                     "est cost"), rows, {"calls", "cache hit", "output", "est cost"})
    lines += ["", "Main vs subagents"]
    total_cost = t["est_cost_usd"] or 0
    rows = [(r["thread"], _n(r["calls"]), _usd(r["est_cost_usd"]),
             _pct(r["est_cost_usd"] / total_cost) if total_cost and r["est_cost_usd"] else "-")
            for r in doc["threads"]]
    lines += _table(("thread", "calls", "est cost", "share"), rows, {"calls", "est cost", "share"})
    lines += ["", "By process"]
    rows = [(r["process"], _n(r["calls"]), _n(r["median_output_tokens"]),
             _pct(r["cache_hit_share"]), _usd(r["est_cost_usd"])) for r in doc["processes"]]
    lines += _table(("process", "calls", "median output", "cache hit", "est cost"), rows,
                    {"calls", "median output", "cache hit", "est cost"})
    lines += ["", "Top projects"]
    rows = [(r["project"], _n(r["calls"]), _usd(r["est_cost_usd"])) for r in doc["top_projects"]]
    lines += _table(("project", "calls", "est cost"), rows, {"calls", "est cost"})
    lines += ["", "By day (UTC)"]
    rows = [(r["date"], _n(r["calls"]), _pct(r["cache_hit_share"]), _usd(r["est_cost_usd"]))
            for r in doc["days"]]
    lines += _table(("date", "calls", "cache hit", "est cost"), rows,
                    {"calls", "cache hit", "est cost"})
    if doc["unpriced_models"]:
        lines += ["", "Unpriced (not in the catalog; not in the cost above)"]
        for r in doc["unpriced_models"]:
            lines.append(f"  {r['model']}: {_n(r['calls'])} calls, {_n(r['tokens'])} tokens")
    if doc["subagent_runs"]:
        lines += ["", "Subagent runs (one run = one subagent invocation)"]
        rows = [(r["process"], _n(r["runs"]), _n(r["calls"]), _n(r["median_run_output_tokens"]),
                 _n(r["p90_run_output_tokens"]), _usd(r["median_run_cost_usd"]),
                 _usd(r["p90_run_cost_usd"]), _usd(r["est_cost_usd"]))
                for r in doc["subagent_runs"]]
        heads = ("type", "runs", "calls", "median output/run", "p90 output/run",
                 "median cost/run", "p90 cost/run", "est cost")
        lines += _table(heads, rows, set(heads[1:]))
    lines += ["", "Where a cheaper model may fit"]
    if not doc["suggestions"]:
        lines.append(f"  nothing stands out (no subagent type whose runs write under "
                     f"{SMALL_RUN_OUTPUT_TOKENS:,} output tokens at the median and "
                     f"{SMALL_RUN_P90_OUTPUT_TOKENS:,} at p90 on a model above Haiku)")
    for s in doc["suggestions"]:
        where = (f"'{s['process']}' subagents" if s["process"] != "subagent"
                 else "subagents of unknown type")
        lines.append(
            f"  {where} ({', '.join(s['models'])}): {_n(s['runs'])} runs, output per run median "
            f"{_n(s['median_run_output_tokens'])} / p90 {_n(s['p90_run_output_tokens'])} tokens, "
            f"cost per run median {_usd(s['median_run_cost_usd'])} / p90 "
            f"{_usd(s['p90_run_cost_usd'])} -> add 'model: haiku' to that subagent's definition; "
            f"saves up to {_usd(s['max_saving_usd'])} of {_usd(s['est_cost_usd'])} "
            f"(upper bound, priced as {s['priced_as']})")
    if doc.get("cold_restarts") is not None:
        lines += [""] + render_cold_restarts(doc["cold_restarts"])
    text = "\n".join(lines) + "\n"
    if doc.get("analysis"):
        from tryaii.claude_code.analysis import render_analysis

        a = doc["analysis"]
        aw = a["window"]
        text += (f"\nTask analysis (tryaii claude-code analyze, {a['created_at'][:10]}; its own "
                 f"window: last {aw['days']} days; computed on this machine)\n")
        text += "".join("  " + line + "\n" if line else "\n"
                        for line in render_analysis(a, heading=False).splitlines())
    return text


_REASON_TEXT = {"expired": "cache expired", "model_switch": "model switch", "other": "other"}


def render_cold_restarts(cold: dict) -> list:
    lines = ["Cold cache restarts (main thread: a call after a pause that wrote the whole "
             "context to the cache again)"]
    if not cold["count"]:
        lines.append(f"  none in {cold['main_sessions']:,} sessions")
        return lines
    reasons = ", ".join(f"{_REASON_TEXT[r['reason']]} {r['count']:,} ({_usd(r['est_cost_usd'])})"
                        for r in cold["by_reason"] if r["count"])
    lines.append(f"  {cold['count']:,} cold restarts in {cold['sessions_with_cold_restarts']:,} of "
                 f"{cold['main_sessions']:,} sessions: {_usd(cold['est_cost_usd'])} above reading "
                 f"the cache (at API list prices)"
                 + (f"; {cold['unpriced']:,} unpriced" if cold["unpriced"] else ""))
    lines.append(f"  by reason: {reasons}")
    rows = [(r["session"], _n(r["cold_restarts"]), _usd(r["est_cost_usd"]))
            for r in cold["sessions"]]
    lines += _table(("session", "cold restarts", "est cost"), rows, {"cold restarts", "est cost"})
    lines.append("  largest:")
    rows = [(r["ts"][:16].replace("T", " "), r["session"], r["model"],
             _REASON_TEXT[r["reason"]], f"{r['gap_minutes']:,.0f} min", _n(r["write_tokens"]),
             _usd(r["est_cost_usd"])) for r in cold["largest"]]
    lines += ["  " + line for line in _table(("time (UTC)", "session", "model", "reason", "pause",
                                               "written", "est cost"), rows,
                                              {"pause", "written", "est cost"})]
    return lines
