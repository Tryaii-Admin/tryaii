"""Sequence (multi-input) analysis: relations between consecutive requests.

For a list of inputs this answers "where does the sequence break, and why":
  - groups requests by (provider, normalized model) — caches are model-scoped,
    so a group boundary is itself a break (SPEC.md delta m)
  - computes the longest common prefix (LCP) between consecutive requests
  - classifies the relation: identical / append-only / truncated / diverged
  - attributes the divergence to a section and a likely cause (which detector
    fires around the break point in both versions)
  - predicts HIT / PARTIAL / MISS / AT_RISK, including TTL-gap checks when
    inputs carry an optional ISO `sent_at` (strict shared grammar, SPEC.md §1.6)
"""

from __future__ import annotations

from tryaii.cachelint import detectors, providers, tokenizers
from tryaii.cachelint._iso8601 import parse_ts_ms
from tryaii.cachelint._jsonutil import normalize_numbers
from tryaii.cachelint.analyzer import AnalyzedItem, _section_of

_DIVERGE_WINDOW_BACK = 40
_DIVERGE_WINDOW_FWD = 100


def _lcp_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _context(text: str, offset: int) -> str:
    lo = max(0, offset - _DIVERGE_WINDOW_BACK)
    hi = min(len(text), offset + _DIVERGE_WINDOW_FWD)
    return text[lo:hi].replace("\n", "\\n")


def _classify_cause(a: str, b: str, lcp: int) -> list[str]:
    """Which detector kinds fire around the divergence point, and did their VALUES change?

    A kind present in both windows with identical matched text is just nearby
    content, not the cause — only value differences (or one-sided presence)
    are reported.
    """
    values: dict[str, dict] = {}
    for label, text in (("a", a), ("b", b)):
        lo = max(0, lcp - _DIVERGE_WINDOW_BACK)
        hi = min(len(text), lcp + _DIVERGE_WINDOW_FWD)
        window = text[lo:hi]
        for f in detectors.scan(window):
            matched = window[f.start:f.end]
            values.setdefault(f.kind, {"a": set(), "b": set()})[label].add(matched)
    causes = []
    for kind, sides in sorted(values.items()):
        if sides["a"] and sides["b"]:
            if sides["a"] != sides["b"]:
                causes.append(f"{kind} value changed")
            # identical values on both sides: nearby content, not the cause
        elif sides["b"]:
            causes.append(f"{kind} introduced")
        elif sides["a"]:
            causes.append(f"{kind} removed")
    return causes


def _pair(prev: AnalyzedItem, curr: AnalyzedItem) -> dict:
    a, b = prev.canonical, curr.canonical
    provider_key = prev.resolved["provider_key"]
    upstream = prev.resolved.get("upstream")
    min_tokens = prev.resolved["min_tokens"]
    spec = prev.resolved["spec"]

    lcp = _lcp_len(a, b)
    lcp_tokens = tokenizers.count_tokens(a[:lcp], provider_key, upstream).tokens
    notes: list[str] = []
    diverged_at = None
    causes: list[str] = []

    if a == b:
        relation = "identical"
    elif lcp == len(a):
        relation = "append-only"       # b extends a: the classic growing conversation
    elif lcp == len(b):
        relation = "truncated"         # b is a prefix of a: history was trimmed
        notes.append("The newer request is a PREFIX of the older one — history trimming? "
                     "Its full length can still hit the cache, but trimming mid-conversation "
                     "usually signals a context-window strategy that fights caching.")
    else:
        relation = "diverged"
        sec = _section_of(prev.sections, min(lcp, max(0, len(a) - 1)))
        causes = _classify_cause(a, b, lcp)
        diverged_at = {
            "offset": lcp,
            "section": sec.name if sec else "?",
            "section_offset": (lcp - sec.start) if sec else None,
            "context_prev": _context(a, lcp),
            "context_curr": _context(b, lcp),
        }
        if sec and sec.name == "tools":
            notes.append("Divergence inside the TOOLS section — tool changes invalidate the "
                         "ENTIRE cache (tools render first).")
        elif sec and sec.name == "system":
            notes.append("Divergence inside the SYSTEM section — invalidates system + messages "
                         "(tools may survive on Anthropic).")

    # expected outcome
    threshold_ok = (min_tokens is None) or (lcp_tokens >= min_tokens)
    if relation in ("identical", "append-only"):
        expected = "HIT" if threshold_ok else "MISS"
        expected_cached = lcp_tokens if threshold_ok else 0
        if not threshold_ok:
            notes.append(f"Common prefix ~{lcp_tokens} tok is below the {min_tokens} minimum — "
                         "no cache credit despite the clean prefix.")
    elif relation == "truncated":
        expected = "HIT" if threshold_ok else "MISS"
        expected_cached = lcp_tokens if threshold_ok else 0
    else:
        if threshold_ok and min_tokens is not None:
            expected = "PARTIAL"
            expected_cached = lcp_tokens
            notes.append(f"Only the ~{lcp_tokens}-tok common prefix gets cache credit; "
                         "everything after the divergence re-processes at full price.")
        elif min_tokens is None:
            expected = "UNKNOWN"
            expected_cached = None
        else:
            expected = "MISS"
            expected_cached = 0
            notes.append(f"Common prefix ~{lcp_tokens} tok < {min_tokens} minimum — "
                         "the divergence effectively kills caching for this pair.")

    # TTL gap check (SPEC.md §1.6: strict shared grammar, epoch-ms arithmetic)
    ttl_check = None
    t_prev, t_curr = parse_ts_ms(prev.sent_at), parse_ts_ms(curr.sent_at)
    if t_prev is not None and t_curr is not None:
        gap = (t_curr - t_prev) / 1000.0
        ttl_check = {"gap_seconds": normalize_numbers(round(gap, 1)),
                     "ttl_seconds": spec.ttl_seconds}
        if spec.ttl_seconds is None:
            ttl_check["note"] = ("No TTL guarantee / unpublished lifetime for this provider — "
                                 "any gap is at-risk.")
            if expected == "HIT":
                expected = "AT_RISK"
        elif gap > spec.ttl_seconds and expected in ("HIT", "PARTIAL"):
            if provider_key == "openai" and gap <= 24 * 3600:
                expected = "AT_RISK"
                ttl_check["note"] = (f"Gap {gap:.0f}s exceeds the 5-10 min sliding window; may "
                                     "still hit via 24h extended retention (not guaranteed).")
            else:
                expected = "MISS"
                ttl_check["note"] = (f"Gap {gap:.0f}s exceeds the default TTL "
                                     f"({spec.ttl_seconds}s) — entry likely evicted. "
                                     "Consider the 1h TTL / keep-alive pings where available.")

    if spec.enablement == "explicit" and expected in ("HIT", "PARTIAL", "AT_RISK"):
        notes.append("Explicit-enablement provider: the hit requires a cache breakpoint "
                     "placed at/inside the common prefix on BOTH requests.")

    return {
        "relation": relation,
        "lcp_chars": lcp,
        "lcp_tokens": lcp_tokens,
        "expected": expected,
        "expected_cached_tokens": expected_cached,
        "diverged_at": diverged_at,
        "likely_causes": causes,
        "ttl_check": ttl_check,
        "notes": notes,
    }


def analyze_sequences(items: list[AnalyzedItem]) -> list[dict]:
    """Group by (provider, normalized model) and analyze consecutive-pair relations."""
    groups: dict[tuple, list] = {}
    order: list[tuple] = []
    for it in items:
        # SPEC.md delta m: normalize the model for grouping; the group header
        # still displays the first-seen raw string.
        key = (it.resolved["provider_key"], providers.norm_model(it.data["model"]))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(it)

    results = []
    for key in order:
        members = groups[key]
        provider_key = key[0]
        model = members[0].data["model"]
        spec = members[0].resolved["spec"]
        min_tokens = members[0].resolved["min_tokens"]

        group_result: dict = {
            "group": {"provider": spec.display, "provider_key": provider_key, "model": model},
            "count": len(members),
            "indices": [m.data["index"] for m in members],
            "pairs": [],
            "shared_prefix_all": None,
            "summary": "",
        }

        if len(members) == 1:
            group_result["summary"] = (
                "Single request — caching needs >= 2 same-prefix requests inside the TTL window "
                "to pay off (a lone write costs the premium for nothing where one exists).")
            results.append(group_result)
            continue

        # LCP across ALL members = the fleet-wide shared cache unit
        shared = members[0].canonical
        shared_len = len(shared)
        for m in members[1:]:
            shared_len = min(shared_len, _lcp_len(shared[:shared_len], m.canonical))
        shared_tokens = tokenizers.count_tokens(
            members[0].canonical[:shared_len],
            provider_key, members[0].resolved.get("upstream")).tokens
        group_result["shared_prefix_all"] = {
            "tokens": shared_tokens,
            "meets_threshold": (min_tokens is None) or (shared_tokens >= min_tokens),
            "min_tokens": min_tokens,
        }

        hits = 0
        for prev, curr in zip(members, members[1:]):
            pair = _pair(prev, curr)
            pair["from"] = prev.data["index"]
            pair["to"] = curr.data["index"]
            group_result["pairs"].append(pair)
            if pair["expected"] in ("HIT", "PARTIAL"):
                hits += 1

        n_pairs = len(group_result["pairs"])
        ok = "clears" if group_result["shared_prefix_all"]["meets_threshold"] else "does NOT clear"
        group_result["summary"] = (
            f"{len(members)} requests; {hits}/{n_pairs} consecutive transitions expected to get "
            f"cache credit. Shared prefix across all requests: ~{shared_tokens} tok, which {ok} "
            f"the activation threshold"
            + (f" ({min_tokens})." if min_tokens is not None else " (unpublished threshold).")
        )
        results.append(group_result)

    if len(order) > 1:
        results.append({
            "group": {"provider": "-", "provider_key": "-", "model": "-"},
            "count": 0, "indices": [], "pairs": [], "shared_prefix_all": None,
            "summary": ("NOTE: the input list spans multiple (provider, model) pairs — caches "
                        "are model- and provider-scoped, so requests in different groups never "
                        "share cache. A model/provider switch mid-sequence = cold cache."),
        })
    return results
