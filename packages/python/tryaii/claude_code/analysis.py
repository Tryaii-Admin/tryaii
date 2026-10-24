"""`tryaii claude-code analyze`: which model is good enough for each kind of
Claude Code task, computed on this machine (shared/monitor/SPEC.md §10.7).

Each task text (``tasks.read_tasks``: a main-thread turn's own message, or a
subagent run's instruction) is embedded with TryAii's local embedding model,
classified against the catalog's benchmark centroids and ranked like a
monitor route (``Router.route``, every model). Two answers per task:

- **the Anthropic answer** (what Claude Code can act on): among the
  Anthropic models of the ranking (and of the data-save policy), those within
  the quality band of the task's model -- ``qualityScore >= anchor - 0.05``
  (``SWAP_QUALITY_EPSILON``) at a blended price ``<=`` the anchor's, the
  winner rule of §2.3 -- form the task's *good-enough set*; the cheapest of
  them is the recommendation (the anchor itself when nothing cheaper fits).
  A task whose model is not in the ranking (unknown to the catalog) has no
  Anthropic answer;
- **the overall winner** (informational): ``pick_winner`` (§2.3) over the
  whole policy-filtered ranking, any provider.

The result is attached to the task's calls: a main turn's calls, or every
call of a subagent run. Advice is aggregated per process (``main`` and each
subagent type), weighted by the tasks' estimated cost: the cheapest
Anthropic model that is in the good-enough set of at least ``ADVICE_SHARE``
of the process's analysed cost, and what moving those calls to it would
have saved. Benchmarks describe short, self-contained tasks better than long
agentic coding sessions, so main-thread advice is always ``low`` confidence.

The stored document (``<data_dir>/claude-code/analysis.json``) holds call
keys (message ids), categories, model ids, counts and costs -- never task
text, embeddings or hashes of the text.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Optional

from tryaii.auth.store import write_atomic
from tryaii.claude_code.pricing import Pricer, claude_code_model
from tryaii.claude_code.tasks import KIND_MAIN, TaskRead
from tryaii.diagnose.modelfit import SWAP_QUALITY_EPSILON, _blended_price
from tryaii.monitor.events import format_ts
from tryaii.monitor.winner import filter_ranking, pick_winner, score_id, score_quality

ANALYSIS_SCHEMA = "tryaii.claude-code.analysis/1"
ANALYSIS_FILE = "analysis.json"
CONSENT_SCHEMA = "tryaii.claude-code.consent/1"
CONSENT_FILE = "consent.json"
#: Bump when what ``analyze`` reads changes: a stored consent of another
#: version is asked again.
CONSENT_VERSION = "2026-10-21"
ANTHROPIC_PREFIX = "anthropic/"
#: A model is advised for a process when it is good enough for at least this
#: share of the process's analysed cost.
ADVICE_SHARE = 0.8
MIN_ADVICE_TASKS = 3
MONTH_DAYS = 30
TOP_CLASSES = 5
CONFIDENCE_NOTE = ("Benchmarks describe short, self-contained tasks (search, summaries, "
                   "lookups, single questions) better than long agentic coding sessions: "
                   "subagent advice fits better than main-thread advice, which is always "
                   "low confidence.")
_FAMILY = re.compile(r"claude-(haiku|sonnet|opus|fable|mythos)")

#: ``route_fn(text) -> (scores, classification)``: the value-ranked ModelScore
#: list (every model) and the ClassificationResult of one task text.
RouteFn = Callable[[str], tuple]


def inprocess_route_fn(bundle, config=None) -> RouteFn:
    """Route in this process with the local embedding model (the monitor's
    model, ``config.embedding_model``) on ``bundle``; no daemon, no network."""
    from tryaii import Router
    from tryaii.monitor.transport import TOP_K

    router = Router(config=config, bundle=bundle, catalog="starter", policy=None)

    def route(text: str):
        result = router.route(text, top_k=TOP_K)
        return result.scores, result.classification

    return route


def family(model_id: Optional[str]) -> Optional[str]:
    """``anthropic/claude-haiku-4.5`` -> ``haiku`` (Claude Code's model alias)."""
    match = _FAMILY.search(model_id or "")
    return match.group(1) if match else None


def good_enough(scores, anchor_id: Optional[str], registry, allowed=None) -> Optional[list]:
    """The Anthropic models within the quality band of ``anchor_id`` at a
    blended price <= its own (§2.3), cheapest first (ties: rank). None when
    the anchor is not ranked or unpriced (no band to search)."""
    ranked = list(scores or [])
    anchor = next((s for s in ranked if score_id(s) == anchor_id), None) if anchor_id else None
    if anchor is None:
        return None
    cap = _blended_price(registry.get_model(anchor_id))
    if cap is None:
        return None
    floor = score_quality(anchor) - SWAP_QUALITY_EPSILON
    out = []
    for rank, s in enumerate(filter_ranking(ranked, allowed)):
        mid = score_id(s)
        if not mid.startswith(ANTHROPIC_PREFIX) or score_quality(s) < floor:
            continue
        price = _blended_price(registry.get_model(mid))
        if price is None or price > cap:
            continue
        out.append((price, rank, mid))
    out.sort()
    return [mid for _p, _r, mid in out]


def _r(value: Optional[float], nd: int = 6) -> Optional[float]:
    return None if value is None else round(value, nd)


def _anchor_model(calls: list, costs: dict) -> str:
    """The model of the task: the one its calls spent most on (then: most calls)."""
    spend: dict = {}
    for c in calls:
        cost = costs.get(c.key)
        total = cost["total"] if cost else 0.0
        n, s = spend.get(c.model, (0, 0.0))
        spend[c.model] = (n + 1, s + total)
    return sorted(spend.items(), key=lambda kv: (-kv[1][1], -kv[1][0], kv[0]))[0][0]


def classify_tasks(calls: list, read: TaskRead, pricer: Pricer, route_fn: RouteFn, *,
                   policy_allowed=None, progress=None) -> tuple[dict, list]:
    """``(per_call, task_rows)``: the result attached to each call key, and
    one internal row per classified task (``process``, ``kind``, ``calls``,
    ``result``). Only tasks with calls in ``calls`` are classified."""
    by_key = {c.key: c for c in calls}
    by_run: dict = {}
    for c in calls:
        if c.subagent and c.run:
            by_run.setdefault(c.run, []).append(c)
    costs = {c.key: pricer.cost(c) for c in calls}
    anthropic = [m for m in pricer.registry.model_ids
                 if m.startswith(ANTHROPIC_PREFIX) and claude_code_model(m)]
    if policy_allowed is not None:
        only = set(policy_allowed)
        anthropic = [m for m in anthropic if m in only]
    work = []
    for task in read.tasks:
        if task.kind == KIND_MAIN:
            mine = [by_key[k] for k in task.call_keys if k in by_key and not by_key[k].subagent]
        else:
            mine = by_run.get(task.ref, [])
        if mine:
            work.append((task, mine))
    per_call: dict = {}
    rows = []
    for n, (task, mine) in enumerate(work):
        if progress:
            progress(n, len(work))
        scores, cls = route_fn(task.text)
        # Variants (``:batch``, ``:free``, ...) are not models Claude Code can use.
        scores = [s for s in scores or [] if claude_code_model(score_id(s))]
        model = _anchor_model(mine, costs)
        anchor_id = pricer.resolve(model)
        overall = pick_winner(scores, anchor_id, pricer.registry, policy_allowed)
        good = good_enough(scores, anchor_id, pricer.registry, anthropic)
        if good is None:
            recommended, method = None, "unranked"
        else:
            recommended = good[0] if good else anchor_id
            method = "anchor" if recommended == anchor_id else "band"
        result = {
            "task": task.kind,
            "category": getattr(cls, "broad_category", "") or None,
            "subcategory": getattr(cls, "subcategory", "") or None,
            "confidence": _r(float(getattr(cls, "confidence", 0.0) or 0.0), 4),
            "anchor": anchor_id,
            "recommended": recommended,
            "method": method,
            "good_enough": good or [],
            "overall_winner": overall["winner"],
            "overall_method": overall["method"],
        }
        for c in mine:
            per_call[c.key] = result
        rows.append({"process": mine[0].process, "kind": task.kind, "calls": mine,
                     "result": result})
    if progress:
        progress(len(work), len(work))
    return per_call, rows


def _class(result: dict) -> str:
    cat, sub = result.get("category"), result.get("subcategory")
    if not cat:
        return "unclassified"
    return f"{cat}/{sub}" if sub else cat


def _advice_text(process: str, kind: str, rec: Optional[dict], current: list, *,
                 tasks: int, unranked_share: float) -> str:
    if rec is None:
        if unranked_share >= 0.5:
            return ("no answer: the model of most of these tasks is not in the TryAii catalog, "
                    "so there is no quality band to compare with")
        if tasks < MIN_ADVICE_TASKS:
            return f"too few tasks for advice ({tasks} < {MIN_ADVICE_TASKS})"
        return ("keep the current model: no cheaper Anthropic model is good enough for "
                f"{int(ADVICE_SHARE * 100)}% of the cost")
    alias = rec["alias"] or rec["model"]
    if all(family(m) == rec["alias"] for m in current):
        return f"already on the cheapest good-enough family ({alias})"
    saving = f"up to ${rec['saving_usd_per_month']:,.2f}/month less"
    if kind == KIND_MAIN:
        return (f"for simple turns, switch with '/model {alias}' (or \"model\": \"{alias}\" in "
                f"settings.json); {saving} (low confidence)")
    if process == "subagent":
        return f"set 'model: {alias}' in those subagents' frontmatter; {saving}"
    return f"set 'model: {alias}' in the {process} subagent's frontmatter; {saving}"


def advise(rows: list, calls: list, pricer: Pricer, *, days: int) -> list:
    """Per process: tasks, cost-weighted class shares, the advised Anthropic
    model and its saving, the most frequent overall winner, the confidence."""
    costs = {c.key: pricer.cost(c) for c in calls}
    process_cost: dict = {}
    for c in calls:
        cost = costs.get(c.key)
        process_cost[c.process] = process_cost.get(c.process, 0.0) + (cost["total"] if cost else 0)
    groups: dict = {}
    for row in rows:
        groups.setdefault(row["process"], []).append(row)
    out = []
    for process, group in groups.items():
        kind = group[0]["kind"]
        weights = [sum(costs[c.key]["total"] for c in r["calls"] if costs.get(c.key))
                   for r in group]
        if sum(weights) <= 0:
            weights = [1.0] * len(group)
        total_w = sum(weights)
        classes: dict = {}
        winners: dict = {}
        candidates: dict = {}
        unranked = 0.0
        for w, r in zip(weights, group):
            res = r["result"]
            if res["method"] == "unranked":
                unranked += w
            classes[_class(res)] = classes.get(_class(res), 0.0) + w
            if res["overall_winner"]:
                winners[res["overall_winner"]] = winners.get(res["overall_winner"], 0.0) + w
            for m in res["good_enough"]:
                candidates[m] = candidates.get(m, 0.0) + w
        analysed = [c for r in group for c in r["calls"]]
        current = sorted({c.model for c in analysed})
        ranked = sorted(candidates.items(),
                        key=lambda kv: (_blended_price(pricer.registry.get_model(kv[0])) or 0,
                                        kv[0]))
        rec = None
        best = None
        for model, w in ranked:
            share = w / total_w
            if best is None or share > best[1]:
                best = (model, share)
            if share >= ADVICE_SHARE and len(group) >= MIN_ADVICE_TASKS:
                saving = 0.0
                for c in analysed:
                    now = costs.get(c.key)
                    alt = pricer.cost(c, model)
                    if now and alt:
                        saving += max(now["total"] - alt["total"], 0.0)
                rec = {"model": model, "alias": family(model), "good_enough_share": round(share, 4),
                       "saving_usd": _r(saving),
                       "saving_usd_per_month": _r(saving * MONTH_DAYS / days)}
                break
        analysed_cost = sum(costs[c.key]["total"] for c in analysed if costs.get(c.key))
        top_winner = max(winners.items(), key=lambda kv: (kv[1], kv[0])) if winners else None
        confidence = "low" if kind == KIND_MAIN or len(group) < MIN_ADVICE_TASKS * 3 else "medium"
        out.append({
            "process": process,
            "thread": "main" if kind == KIND_MAIN else "subagent",
            "tasks": len(group),
            "calls": len(analysed),
            "models": current,
            "est_cost_usd": _r(analysed_cost),
            "analysed_cost_share": (round(analysed_cost / process_cost[process], 4)
                                    if process_cost.get(process) else None),
            "classes": [{"class": k, "share": round(v / total_w, 4)} for k, v in
                        sorted(classes.items(), key=lambda kv: (-kv[1], kv[0]))][:TOP_CLASSES],
            "good_enough_shares": [{"model": m, "share": round(w / total_w, 4)}
                                   for m, w in ranked],
            "unranked_share": round(unranked / total_w, 4),
            "recommendation": rec,
            "best_candidate": (None if rec or best is None else
                               {"model": best[0], "good_enough_share": round(best[1], 4)}),
            "overall_winner": (None if top_winner is None else
                               {"model": top_winner[0],
                                "share": round(top_winner[1] / total_w, 4)}),
            "confidence": confidence,
            "advice": _advice_text(process, kind, rec, current, tasks=len(group),
                                   unranked_share=unranked / total_w),
        })
    out.sort(key=lambda r: (-(r["est_cost_usd"] or 0), r["process"]))
    return out


def build_analysis(calls: list, read: TaskRead, pricer: Pricer, route_fn: RouteFn, *,
                   days: int, now: datetime, since: datetime, consent: dict,
                   embedding_model: Optional[str], taxonomy_version: Optional[str],
                   policy_allowed=None, progress=None) -> dict:
    per_call, rows = classify_tasks(calls, read, pricer, route_fn,
                                    policy_allowed=policy_allowed, progress=progress)
    counts = dict(read.counts)
    counts.update({
        "tasks_classified": len(rows),
        "main_turns_classified": sum(1 for r in rows if r["kind"] == KIND_MAIN),
        "subagent_runs_classified": sum(1 for r in rows if r["kind"] != KIND_MAIN),
        "calls": len(calls),
        "calls_with_result": len(per_call),
    })
    return {
        "schema": ANALYSIS_SCHEMA,
        "created_at": format_ts(now),
        "window": {"days": days, "start": format_ts(since), "end": format_ts(now)},
        "consent": consent,
        "catalog_version": pricer.catalog_version,
        "taxonomy_version": taxonomy_version,
        "embedding_model": embedding_model,
        "cost_basis": "estimate at API list prices",
        "rule": (f"Anthropic answer: the cheapest Anthropic model with quality >= the task "
                 f"model's - {SWAP_QUALITY_EPSILON} and blended price <= its own; advice: the "
                 f"cheapest model good enough for >= {int(ADVICE_SHARE * 100)}% of a process's "
                 f"analysed cost (at least {MIN_ADVICE_TASKS} tasks)"),
        "confidence_note": CONFIDENCE_NOTE,
        "counts": counts,
        "processes": advise(rows, calls, pricer, days=days),
        "calls": per_call,
    }


# --- local state ----------------------------------------------------------------

def _state_dir(data_dir) -> Path:
    from tryaii.claude_code.importer import STATE_DIR

    return Path(data_dir) / STATE_DIR


def analysis_path(data_dir) -> Path:
    return _state_dir(data_dir) / ANALYSIS_FILE


def consent_path(data_dir) -> Path:
    return _state_dir(data_dir) / CONSENT_FILE


def _write_json(path: Path, doc: dict, indent: Optional[int] = 2) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(doc, indent=indent, ensure_ascii=False)
    write_atomic(path, (text + "\n").encode("utf-8"))
    return path


def _read_json(path: Path, schema: str) -> Optional[dict]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("schema") == schema else None


def read_consent(data_dir) -> Optional[dict]:
    """The stored consent of the current version, or None."""
    doc = _read_json(consent_path(data_dir), CONSENT_SCHEMA)
    return doc if doc and doc.get("version") == CONSENT_VERSION else None


def write_consent(data_dir, *, now: str, method: str) -> dict:
    doc = {"schema": CONSENT_SCHEMA, "scope": "claude-code analyze", "version": CONSENT_VERSION,
           "granted_at": now, "method": method,
           "reads": "the text of your own Claude Code messages and subagent instructions, "
                    "on this machine only"}
    _write_json(consent_path(data_dir), doc)
    return doc


def write_analysis(data_dir, doc: dict) -> Path:
    return _write_json(analysis_path(data_dir), doc, indent=None)  # one entry per call


def read_analysis(data_dir) -> Optional[dict]:
    doc = _read_json(analysis_path(data_dir), ANALYSIS_SCHEMA)
    return drop_variants(doc) if doc else None


def drop_variants(doc: dict) -> dict:
    """An analysis without catalog variants Claude Code cannot use
    (``pricing.claude_code_model``): an analysis stored before 0.8.3 may name
    ``anthropic/claude-opus-4.7:batch`` beside ``anthropic/claude-opus-4.7``
    (one display name twice). Variants leave the good-enough lists; a call
    whose answer was a variant gets the cheapest usable model of its list (or
    its own model); a process advice that named one is withdrawn until the
    next ``analyze``."""
    for result in (doc.get("calls") or {}).values():
        if not isinstance(result, dict):
            continue
        good = [m for m in result.get("good_enough") or [] if claude_code_model(m)]
        result["good_enough"] = good
        rec = result.get("recommended")
        if rec is not None and not claude_code_model(rec):
            result["recommended"] = good[0] if good else result.get("anchor")
            result["method"] = "band" if result["recommended"] != result.get("anchor") \
                else "anchor"
        if result.get("overall_winner") is not None \
                and not claude_code_model(result["overall_winner"]):
            result["overall_winner"] = None
    for row in doc.get("processes") or []:
        if not isinstance(row, dict):
            continue
        row["good_enough_shares"] = [g for g in row.get("good_enough_shares") or []
                                     if isinstance(g, dict)
                                     and claude_code_model(g.get("model"))]
        for name in ("best_candidate", "overall_winner"):
            value = row.get(name)
            if isinstance(value, dict) and not claude_code_model(value.get("model")):
                row[name] = None
        rec = row.get("recommendation")
        if isinstance(rec, dict) and not claude_code_model(rec.get("model")):
            row["recommendation"] = None
            row["advice"] = ("no advice: this analysis named a model variant Claude Code "
                             "cannot use; run tryaii claude-code analyze again")
    return doc


def call_results(data_dir) -> dict:
    """call key -> stored result (empty without an analysis)."""
    doc = read_analysis(data_dir)
    calls = doc.get("calls") if doc else None
    return calls if isinstance(calls, dict) else {}


def summary(doc: dict) -> dict:
    """The analysis without the per-call map (for output and the report)."""
    return {k: v for k, v in doc.items() if k != "calls"}


# --- text ----------------------------------------------------------------------

def _pct(value) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def render_analysis(doc: dict, *, heading: bool = True) -> str:
    w = doc["window"]
    c = doc["counts"]
    lines = []
    if heading:
        lines += [f"Claude Code task analysis -- last {w['days']} days "
                  f"({w['start'][:10]} .. {w['end'][:10]}), computed on this machine",
                  f"catalog {doc.get('catalog_version') or '-'}, embedding model "
                  f"{doc.get('embedding_model') or '-'}; costs: {doc['cost_basis']}", ""]
    lines += [
        f"tasks classified: {c['tasks_classified']:,} ({c['main_turns_classified']:,} main "
        f"turns, {c['subagent_runs_classified']:,} subagent runs); calls with a result: "
        f"{c['calls_with_result']:,} of {c['calls']:,}",
        f"skipped: {c['skipped_secret']:,} that looked like secrets, "
        f"{c['skipped_tool_result_only']:,} tool-result lines, {c['skipped_injected']:,} "
        f"injected-context lines, {c['runs_without_instruction']:,} runs without an instruction",
    ]
    for p in doc["processes"]:
        lines.append("")
        classes = ", ".join(f"{x['class']} {_pct(x['share'])}" for x in p["classes"])
        noun = "task" if p["tasks"] == 1 else "tasks"
        lines.append(f"{p['process']} ({p['thread']}; {p['tasks']:,} {noun}, {p['calls']:,} "
                     f"calls, ${p['est_cost_usd'] or 0:,.2f}; models {', '.join(p['models'])}; "
                     f"confidence {p['confidence']})")
        lines.append(f"  classes by cost: {classes or '-'}")
        shares = ", ".join(f"{x['model']} {_pct(x['share'])}" for x in p["good_enough_shares"])
        lines.append(f"  good enough (share of cost): {shares or '-'}"
                     + (f"; no band (model not in the catalog): {_pct(p['unranked_share'])}"
                        if p["unranked_share"] else ""))
        rec = p["recommendation"]
        if rec:
            lines.append(f"  {rec['model']} is good enough for {_pct(rec['good_enough_share'])} "
                         f"of the cost; saving ${rec['saving_usd']:,.2f} in the window, "
                         f"${rec['saving_usd_per_month']:,.2f}/month")
        elif p["best_candidate"]:
            b = p["best_candidate"]
            lines.append(f"  closest: {b['model']} good enough for "
                         f"{_pct(b['good_enough_share'])} of the cost "
                         f"(advice needs {int(ADVICE_SHARE * 100)}% and {MIN_ADVICE_TASKS} tasks)")
        if p["overall_winner"]:
            o = p["overall_winner"]
            lines.append(f"  overall winner, any provider (informational): {o['model']} "
                         f"({_pct(o['share'])})")
        lines.append(f"  -> {p['advice']}")
    lines += ["", f"note: {doc['confidence_note']}"]
    return "\n".join(lines) + "\n"
