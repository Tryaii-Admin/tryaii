"""Single-prompt cache analysis.

Pipeline per input:
  1. canonical render (tools -> system -> messages, the documented provider order)
  2. token counts (total + per section, method-labeled)
  3. dynamic-content findings with section attribution
  4. stable-prefix computation (tokens before the first blocking finding)
  5. verdict + provider-aware recommendations
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from tryaii.cachelint import detectors, providers, tokenizers
from tryaii.cachelint._jsonutil import sorted_minified_dumps


@dataclass
class Section:
    name: str
    start: int
    end: int


@dataclass
class AnalyzedItem:
    """Analysis result plus internals the sequence analyzer reuses."""
    data: dict
    canonical: str
    sections: list
    resolved: dict
    sent_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Input normalization + canonical rendering
# ---------------------------------------------------------------------------

def _extract_text(content: Any) -> tuple[str, int]:
    """Flatten message content into text; count non-text blocks (images etc.)."""
    if content is None:
        return "", 0
    if isinstance(content, str):
        return content, 0
    if isinstance(content, list):
        parts, non_text = [], 0
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text" or "text" in block:
                    parts.append(str(block.get("text", "")))
                else:
                    non_text += 1
                    parts.append(f"[{block.get('type', 'block')}]")
            else:
                parts.append(str(block))
        return "\n".join(parts), non_text
    return str(content), 0


def build_canonical(prompt: Any) -> tuple[str, list, int]:
    """Render the prompt into one deterministic string with section spans.

    Order: tools -> system -> messages (the Anthropic-documented render order;
    used as the canonical approximation for all providers).
    Returns (text, sections, non_text_block_count).
    """
    if isinstance(prompt, str):
        prompt = {"messages": [{"role": "user", "content": prompt}]}
    if not isinstance(prompt, dict):
        raise ValueError("prompt must be a string or an object with system/messages/tools")

    pieces: list[tuple[str, str]] = []
    non_text_total = 0

    tools = prompt.get("tools")
    if tools:
        # Canonical serialization: minified, sorted keys, integral floats as
        # ints (SPEC.md §1.4) — byte-identical to the Node SDK's rendering.
        pieces.append(("tools", sorted_minified_dumps(tools)))

    system = prompt.get("system")
    if system:
        text, nt = _extract_text(system)
        non_text_total += nt
        pieces.append(("system", text))

    for i, msg in enumerate(prompt.get("messages") or []):
        if isinstance(msg, dict):
            role = msg.get("role", "user")
            text, nt = _extract_text(msg.get("content"))
            non_text_total += nt
        else:
            role, text = "user", str(msg)
        pieces.append((f"messages[{i}]:{role}", f"{role}: {text}"))

    sections: list[Section] = []
    out: list[str] = []
    pos = 0
    for name, text in pieces:
        if out:
            out.append("\n\n")
            pos += 2
        start = pos
        out.append(text)
        pos += len(text)
        sections.append(Section(name, start, pos))
    return "".join(out), sections, non_text_total


def _section_of(sections: list, offset: int) -> Optional[Section]:
    """Section containing `offset`; separator / past-end offsets map to the
    nearest PRECEDING section (SPEC.md delta l)."""
    prev: Optional[Section] = None
    for sec in sections:
        if offset < sec.start:
            break
        prev = sec
        if sec.start <= offset < max(sec.end, sec.start + 1):
            return sec
    return prev if prev is not None else (sections[0] if sections else None)


# ---------------------------------------------------------------------------
# Verdict + recommendations
# ---------------------------------------------------------------------------

_FIX_HINTS = {
    "timestamp-datetime": "Remove it, round it to a coarse bucket (day/hour), or move it into the final user message (after the cached prefix).",
    "timestamp-epoch": "Remove the epoch value or move it after the static prefix.",
    "uuid": "Move per-session IDs after the static prefix — or drop them from the prompt entirely.",
    "session-id": "Move per-session/user IDs to the tail (final user message); they prevent any cross-request prefix reuse where they sit.",
    "secret-like": "Remove the secret from the prompt (pass credentials out-of-band). This is a security issue independent of caching.",
    "date-phrase": "Inject 'today' only in the final user message, or round to the day and accept a daily cold write.",
    "date-iso": "If this is an injected 'today', move it to the tail; if it's static content, ignore this finding.",
    "date-us": "If this is an injected 'today', move it to the tail; if it's static content, ignore this finding.",
    "date-verbose": "If this is an injected 'today', move it to the tail; if it's static content, ignore this finding.",
    "template-jinja": "Template slot: everything BEFORE it stays cacheable — keep all slots that vary per call at the tail of the prompt.",
    "template-braces": "Template slot: keep per-call slots at the tail; slots filled with the SAME value every call are harmless.",
    "template-dollar": "Template slot: keep per-call slots at the tail of the prompt.",
    "template-percent-named": "Template slot: keep per-call slots at the tail of the prompt.",
    "template-angle": "Placeholder: keep per-call placeholders at the tail of the prompt.",
    "template-bracket": "Placeholder: fill it with a stable value or keep it at the tail.",
}


def _verdict(resolved: dict, total_tokens: int, stable_tokens: int,
             has_blockers: bool) -> tuple[str, str]:
    spec = resolved["spec"]
    min_tokens = resolved["min_tokens"]
    if min_tokens is None:
        return ("UNKNOWN_THRESHOLD",
                f"{spec.display} publishes no activation threshold — analysis is structural only. "
                f"Stable prefix ~{stable_tokens} tok of {total_tokens} total.")
    if total_tokens < min_tokens:
        return ("BELOW_THRESHOLD",
                f"Total prompt ~{total_tokens} tok < {min_tokens} minimum — nothing will cache. "
                "Expanding the static content to reach the floor is often worthwhile.")
    if has_blockers and stable_tokens < min_tokens:
        return ("EFFECTIVELY_UNCACHEABLE",
                f"Dynamic content too early: the stable prefix is only ~{stable_tokens} tok, "
                f"below the {min_tokens} minimum — in practice nothing will cache until it moves.")
    if has_blockers:
        return ("CACHEABLE_PREFIX",
                f"A stable prefix of ~{stable_tokens} tok (>= {min_tokens} minimum) can cache; "
                "the dynamic tail after it re-processes at full price each call (that part is normal).")
    # SPEC.md delta i: any Anthropic upstream needs the explicit cache_control
    # action — pass-through (openrouter) AND hybrid (vertex+claude) alike.
    needs_action = spec.enablement == "explicit" or resolved.get("upstream") == "anthropic"
    if needs_action:
        who = spec.display if spec.enablement == "explicit" else \
            f"{spec.display} (Anthropic upstream)"
        return ("CACHEABLE_WITH_ACTION",
                f"~{total_tokens} tok, no dynamic content detected — cacheable, but {who} "
                "requires an explicit opt-in (see enablement).")
    return ("CACHEABLE",
            f"~{total_tokens} tok of stable content, above the {min_tokens} minimum — should cache.")


def _recommendations(resolved: dict, verdict_code: str, findings: list,
                     stable_tokens: int, next_stable: Optional[int]) -> list[str]:
    spec = resolved["spec"]
    recs: list[str] = []

    if spec.action:
        recs.append(f"[enablement] {spec.action}")

    blockers = detectors.blocking(findings)
    for f in blockers[:6]:
        hint = _FIX_HINTS.get(f.kind, "Move this value after the static prefix.")
        recs.append(f"[fix:{f.kind}] at {f.section}+{f.section_offset} "
                    f"({f.pct_into_prompt:.0f}% in): {hint}")
    if len(blockers) > 6:
        recs.append(f"[fix] ...and {len(blockers) - 6} more findings (see the findings list).")

    if blockers and next_stable is not None:
        recs.append(f"[impact] Fixing the FIRST finding alone extends the stable prefix from "
                    f"~{stable_tokens} to ~{next_stable} tok.")

    if spec.routing:
        recs.append(f"[routing] {spec.routing}")

    recs.append(f"[verify] After deploying, assert {spec.verify_field} > 0 in responses. "
                "0 across identical-prefix requests = a silent invalidator is still present.")

    if verdict_code in ("CACHEABLE", "CACHEABLE_WITH_ACTION", "CACHEABLE_PREFIX"):
        recs.append(f"[ttl] {spec.ttl_summary}")
    if spec.batch_note:
        recs.append(f"[batch] {spec.batch_note}")
    return recs


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def _next_blocking_start(blockers: list, first) -> Optional[int]:
    """Start offset of the earliest blocker OTHER than `first` — excluded by
    identity, not by offset, so a co-located second blocker still counts
    (SPEC.md delta g)."""
    rest = [b for b in blockers if b is not first]
    return min(rest, key=lambda f: f.start).start if rest else None


def analyze_item(item: dict, index: int = 0) -> AnalyzedItem:
    """Analyze one {prompt, llm:{provider,name}} input."""
    llm = item.get("llm") or {}
    provider = llm.get("provider") or item.get("provider") or ""
    model = llm.get("name") or llm.get("model") or item.get("model") or ""
    if not provider:
        raise ValueError(f"input #{index}: missing llm.provider")

    resolved = providers.resolve(provider, model)
    spec = resolved["spec"]
    upstream = resolved.get("upstream")

    canonical, sections, non_text = build_canonical(item.get("prompt"))

    # token counts: total + per section
    total_tc = tokenizers.count_tokens(canonical, resolved["provider_key"], upstream)
    section_counts = {}
    for sec in sections:
        tc = tokenizers.count_tokens(canonical[sec.start:sec.end], resolved["provider_key"], upstream)
        section_counts[sec.name] = tc.tokens

    # findings with section attribution
    findings = detectors.scan(canonical)
    for f in findings:
        sec = _section_of(sections, f.start)
        if sec:
            f.section = sec.name
            f.section_offset = f.start - sec.start

    blockers = detectors.blocking(findings)
    first = detectors.first_blocking(findings)
    if first is not None:
        stable_tc = tokenizers.count_tokens(canonical[:first.start], resolved["provider_key"], upstream)
        stable_tokens = stable_tc.tokens
        # hypothetical: prefix if the first blocker were fixed
        nxt_start = _next_blocking_start(blockers, first)
        if nxt_start is not None:
            next_stable = tokenizers.count_tokens(
                canonical[:nxt_start], resolved["provider_key"], upstream).tokens
        else:
            next_stable = total_tc.tokens
    else:
        stable_tokens = total_tc.tokens
        next_stable = None

    # the classic cached unit: tools + system
    cached_unit_tokens = sum(
        n for name, n in section_counts.items() if name in ("tools", "system"))
    system_findings = [f for f in findings if f.section == "system"]

    min_tokens = resolved["min_tokens"]
    verdict_code, verdict_summary = _verdict(
        resolved, total_tc.tokens, stable_tokens, bool(blockers))
    recs = _recommendations(resolved, verdict_code, findings, stable_tokens, next_stable)

    warnings = list(resolved["warnings"])
    if non_text:
        warnings.append(f"{non_text} non-text block(s) (images/documents) approximated as "
                        "placeholders — token counts underestimate them.")
    if not total_tc.exact:
        warnings.append(f"Token counts are estimates ({total_tc.method}). {total_tc.note}")

    data = {
        "index": index,
        "provider": spec.display,
        "provider_key": resolved["provider_key"],
        "upstream": upstream,
        "model": model,
        "token_report": {
            "total": total_tc.to_dict(),
            "sections": section_counts,
        },
        "threshold": {
            "min_tokens": min_tokens,
            "tier": resolved["tier_note"],
            "increment": spec.increment_tokens,
            "meets": (min_tokens is not None and total_tc.tokens >= min_tokens),
        },
        "system_report": {
            "present": any(s.name == "system" for s in sections),
            "tokens": section_counts.get("system", 0),
            "cached_unit_tokens": cached_unit_tokens,   # tools + system
            "cached_unit_meets_threshold": (
                min_tokens is not None and cached_unit_tokens >= min_tokens),
            "findings_in_system": len(system_findings),
        },
        "findings": [f.to_dict() for f in findings],
        "stable_prefix": {
            "tokens": stable_tokens,
            "pct_of_prompt": round(100.0 * stable_tokens / max(1, total_tc.tokens), 1),
            "limited_by": first.to_dict() if first else None,
            "if_first_fixed_tokens": next_stable,
        },
        "verdict": {"code": verdict_code, "summary": verdict_summary},
        "enablement": {
            "mode": spec.enablement,
            "detail": spec.enablement_detail,
            "action": spec.action,
        },
        "provider_intel": {
            "ttl": spec.ttl_summary,
            "read_discount": spec.read_discount,
            "write_cost": spec.write_cost,
            "verify_field": spec.verify_field,
            "routing": spec.routing,
            "batch": spec.batch_note,
            "gotchas": list(spec.gotchas),
            "sources": list(spec.sources),
        },
        "recommendations": recs,
        "warnings": warnings,
    }
    return AnalyzedItem(
        data=data, canonical=canonical, sections=sections,
        resolved=resolved, sent_at=item.get("sent_at"),
    )
