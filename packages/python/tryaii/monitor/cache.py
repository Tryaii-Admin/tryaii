"""Cache fields of an event (API.md v1.4 §3.2.1, SPEC.md §3.1).

Two fields:

* ``usage.cached_tokens`` — the provider's cached prompt tokens, read from the
  response usage (:func:`cached_tokens_from_usage`); recorded on every call.
* ``cache`` — only when the call ran the runtime cache lint
  (``wrap(client, cachelint=True)``): ``{"outcome", "report"}``, built per
  process by :class:`CacheTracker`.

Redaction: the engine's own finding ``excerpt`` (the match plus 28 code points
of context) is never recorded. A report carries :func:`mask_excerpt` of the
matched span only, so neither the local events file nor the upload ever holds
prompt text. The pure helpers here are mirrored by the Node SDK's
``monitor/cache.ts`` and pinned by shared/monitor/fixtures/cache.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

#: Contract limits (API.md §3.2.1).
MAX_FINDINGS = 10
MAX_DETECTOR = 64
MAX_EXCERPT = 40
MAX_RECOMMENDATION = 500
ELLIPSIS = "…"
#: Minimum gap between two full analyses (so two reports) of one process (s).
REPORT_INTERVAL_S = 60.0
#: Processes remembered per tracker (FIFO cap).
MAX_PROCESSES = 256

SECTION_KINDS = ("tools", "system", "messages")
OUTCOMES = {"HIT": "hit", "PARTIAL": "partial", "MISS": "miss"}
VERDICTS = ("CACHEABLE", "CACHEABLE_WITH_ACTION", "CACHEABLE_PREFIX",
            "EFFECTIVELY_UNCACHEABLE", "BELOW_THRESHOLD", "UNKNOWN_THRESHOLD")
SEVERITIES = ("high", "medium", "low")
#: Whitespace per the mask rule: the Unicode White_Space code points, listed
#: explicitly (the same 25 in both SDKs; no Unicode table is consulted).
_WHITESPACE = frozenset(
    "\t\n\x0b\x0c\r \x85\xa0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000")
#: The engine's label of a system-role message (OpenAI ``system``/``developer``).
_SYSTEM_MESSAGE = re.compile(r"messages\[[0-9]+\]:(?:system|developer)")


def _count(value: Any) -> Optional[int]:
    """A non-negative integer count, or None (bools and negatives refused)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value < 0:  # NaN, negative
        return None
    try:
        return int(value)
    except (OverflowError, ValueError):
        return None


def _field(obj: Any, key: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def cached_tokens_from_usage(usage: Any) -> Optional[int]:
    """``usage.prompt_tokens_details.cached_tokens`` (OpenAI-compatible) or
    ``usage.cache_read_input_tokens`` (Anthropic-shaped); None when the
    provider reported neither (0 = reported, no hit)."""
    if usage is None:
        return None
    cached = _count(_field(_field(usage, "prompt_tokens_details"), "cached_tokens"))
    if cached is not None:
        return cached
    return _count(_field(usage, "cache_read_input_tokens"))


def mask_excerpt(text: str) -> str:
    """The matched span, per code point (API.md §3.2.1): ASCII digit → ``0``;
    whitespace → a space; printable ASCII punctuation/symbols (``!``..``~``
    minus letters and digits) kept; every other code point (ASCII letters,
    any non-ASCII letter, digit, combining mark, emoji or symbol, controls)
    → ``x``. At most 40 code points (39 + ``…`` when cut)."""
    out = []
    for ch in text:
        if "0" <= ch <= "9":
            out.append("0")
        elif ch in _WHITESPACE:
            out.append(" ")
        elif "!" <= ch <= "~" and not ("a" <= ch <= "z" or "A" <= ch <= "Z"):
            out.append(ch)
        else:
            out.append("x")
    if len(out) > MAX_EXCERPT:
        out = out[:MAX_EXCERPT - 1] + [ELLIPSIS]
    return "".join(out)


def section_kind(label: Any) -> str:
    """The report section of an engine section label (API.md §3.2.1): a
    message whose role is ``system`` or ``developer`` (``messages[0]:system``,
    ``messages[2]:developer``) → ``system``; otherwise the label cut at the
    first ``[`` or ``:`` (``tools`` → tools, ``system`` → system,
    ``messages[2]:user`` and anything unexpected → messages)."""
    text = str(label or "")
    if _SYSTEM_MESSAGE.fullmatch(text):
        return "system"
    for sep in ("[", ":"):
        idx = text.find(sep)
        if idx >= 0:
            text = text[:idx]
    return text if text in SECTION_KINDS else "messages"


def _untag(line: str) -> str:
    idx = line.find("] ")
    return line[idx + 2:] if idx >= 0 else line


def recommendation_from(recommendations: Any, verdict: str) -> Optional[str]:
    """The first ``[fix:*]`` line, else (CACHEABLE_WITH_ACTION only) the
    ``[enablement]`` line, with its ``[tag] `` removed; None otherwise.
    Capped at 500 code points."""
    lines = [r for r in (recommendations or []) if isinstance(r, str)]
    chosen = next((r for r in lines if r.startswith("[fix:")), None)
    if chosen is None and verdict == "CACHEABLE_WITH_ACTION":
        chosen = next((r for r in lines if r.startswith("[enablement]")), None)
    if chosen is None:
        return None
    text = _untag(chosen)
    if len(text) > MAX_RECOMMENDATION:
        text = text[:MAX_RECOMMENDATION - 1] + ELLIPSIS
    return text


def outcome_from_expected(expected: Any) -> Optional[str]:
    """Pair prediction → event outcome (UNKNOWN, AT_RISK → None)."""
    return OUTCOMES.get(expected) if isinstance(expected, str) else None


def lint_prompt(messages: Any, tools: Any = None, functions: Any = None) -> dict:
    """The cachelint prompt of one chat.completions request: ``messages``,
    plus ``tools`` when the request declares tool schemas (its ``tools``
    list followed by the legacy ``functions`` list). The engine renders
    ``tools`` first (shared/cachelint/SPEC.md §1.4), so the schemas count in
    ``prompt_tokens``, ``tools_tokens``, the stable prefix and the verdict."""
    prompt: dict = {"messages": messages}
    schemas = [*(tools if isinstance(tools, list) else []),
               *(functions if isinstance(functions, list) else [])]
    if schemas:
        prompt["tools"] = schemas
    return prompt


def build_report(data: dict, canonical: str) -> dict:
    """A CacheReport (API.md §3.2.1) from one analysed item (the engine's
    ``items[i]`` dict) and the canonical text it was computed on."""
    tokens = data.get("token_report") or {}
    sections = {kind: 0 for kind in SECTION_KINDS}
    for label, n in (tokens.get("sections") or {}).items():
        sections[section_kind(label)] += _count(n) or 0
    findings = []
    for f in (data.get("findings") or [])[:MAX_FINDINGS]:
        start, end = int(f["start"]), int(f["end"])
        severity = f.get("severity")
        findings.append({
            "severity": severity if severity in SEVERITIES else "low",
            "detector": str(f.get("kind") or "")[:MAX_DETECTOR],
            "section": section_kind(f.get("section")),
            "offset": max(0, start),
            "excerpt": mask_excerpt(canonical[start:end]),
        })
    verdict = (data.get("verdict") or {}).get("code")
    threshold = data.get("threshold") or {}
    return {
        "verdict": verdict,
        "prompt_tokens": _count((tokens.get("total") or {}).get("tokens")) or 0,
        "stable_prefix_tokens": _count((data.get("stable_prefix") or {}).get("tokens")) or 0,
        "floor_tokens": _count(threshold.get("min_tokens")),
        "tools_tokens": sections["tools"],
        "system_tokens": sections["system"],
        "messages_tokens": sections["messages"],
        "findings": findings,
        "recommendation": recommendation_from(data.get("recommendations"), verdict),
    }


# ---------------------------------------------------------------------------
# Per-process tracking
# ---------------------------------------------------------------------------

LINT_PROVIDER = "openrouter"   # the provider the runtime hook lints as


@dataclass
class _ProcessState:
    prev: Any = None                 # cachelint AnalyzedItem of the previous linted call
    prev_group: Optional[str] = None
    report: Optional[dict] = None    # last report attached
    report_key: Optional[str] = None  # shape of the last analysed call
    analysed_at: Optional[float] = None  # clock of the last full analysis


def _shape_key(model: str, canonical: str) -> str:
    return hashlib.sha256((model + "\x00" + canonical).encode("utf-8")).hexdigest()


class CacheTracker:
    """The ``cache`` block per process (API.md §3.2.1): the pair prediction
    from the previous linted call of the same process, and a report on the
    first call and then when it changed and 60 s have passed.

    The full analysis (``analyze_item``: detectors, token report, verdict)
    runs on the first call of a process, then at most once per 60 s per
    process and only when the prompt's shape changed, whether or not the
    resulting report differs from the last one sent. The pair prediction
    runs on every call; it is the cheap path (a common-prefix scan and one
    token count of that prefix, no detectors). ``analyses`` counts full
    analyses. Thread-safe; the tracker lives as long as its Monitor (one SDK
    process)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._procs: dict[str, _ProcessState] = {}
        #: Full analyses run so far (all processes).
        self.analyses = 0

    def observe(self, process: str, model: str, messages: list,
                tools: Any = None, functions: Any = None) -> dict:
        from tryaii.cachelint import providers
        from tryaii.cachelint.analyzer import AnalyzedItem, analyze_item, build_canonical
        from tryaii.cachelint.sequence import predict_pair

        prompt = lint_prompt(messages, tools, functions)
        canonical, sections, _ = build_canonical(prompt)
        resolved = providers.resolve(LINT_PROVIDER, model)
        group = providers.norm_model(model)
        curr = AnalyzedItem(data={"index": 0, "model": model}, canonical=canonical,
                            sections=sections, resolved=resolved)
        key = _shape_key(model, canonical)
        with self._lock:
            state = self._procs.get(process)
            if state is None:
                if len(self._procs) >= MAX_PROCESSES:
                    self._procs.pop(next(iter(self._procs)))
                state = _ProcessState()
                self._procs[process] = state
            prev, prev_group = state.prev, state.prev_group
            state.prev, state.prev_group = curr, group
            now = self._clock()
            # At most one full analysis per 60 s per process: the slot is
            # claimed here, under the lock, whether or not the report it
            # produces differs from the last one sent.
            analyse = state.analysed_at is None or (
                now - state.analysed_at >= REPORT_INTERVAL_S and key != state.report_key)
            if analyse:
                state.analysed_at = now
                state.report_key = key
                self.analyses += 1
        outcome = None
        if prev is not None and prev_group == group:
            outcome = outcome_from_expected(predict_pair(prev, curr).get("expected"))
        report = None
        if analyse:
            item = analyze_item({"prompt": prompt,
                                 "llm": {"provider": LINT_PROVIDER, "name": model}})
            candidate = build_report(item.data, item.canonical)
            with self._lock:
                if state.report is None or candidate != state.report:
                    report = candidate
                    state.report = candidate
        return {"outcome": outcome, "report": report}
