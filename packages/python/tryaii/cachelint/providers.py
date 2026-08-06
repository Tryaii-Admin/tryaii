"""Provider caching knowledge base — loader + resolution.

The knowledge base itself is DATA: `data/providers.json`, synced from the
repo's `shared/cachelint/providers.json` master (single source of truth,
byte-compared across both SDKs by test_parity). This module loads it and
resolves (provider, model) into an effective spec + minimum cacheable tokens,
including gateway upstream delegation (openrouter -> anthropic/openai/gemini/
xai tiers, vertex+claude -> anthropic tiers).

All thresholds/prices are time-sensitive; refresh policy in SPEC.md §5.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class ThresholdRule:
    """First matching rule (in order) decides the model's minimum cacheable tokens."""

    pattern: str                 # regex vs the normalized model name
    min_tokens: Optional[int]    # None = unpublished/unknown
    note: str = ""


@dataclass(frozen=True)
class ProviderSpec:
    key: str
    display: str
    # "automatic" | "explicit" | "hybrid" | "pass-through"
    enablement: str
    enablement_detail: str
    # Concrete action the user must take (None if nothing is required).
    action: Optional[str]
    threshold_rules: tuple = ()
    default_min_tokens: Optional[int] = None
    default_min_note: str = ""
    increment_tokens: Optional[int] = None   # e.g. OpenAI's 128-token steps
    ttl_summary: str = ""
    ttl_seconds: Optional[int] = None        # conservative TTL for gap checks; None = no guarantee
    read_discount: str = ""
    write_cost: str = ""
    verify_field: str = ""
    routing: Optional[str] = None            # cache-key / routing knob
    batch_note: str = ""
    gotchas: tuple = ()
    sources: tuple = ()


def _load_kb() -> dict:
    data = json.loads(
        (Path(__file__).parent / "data" / "providers.json").read_text(encoding="utf-8"))
    rule_sets = {
        name: tuple(ThresholdRule(r["pattern"], r["min_tokens"], r["note"])
                    for r in rules)
        for name, rules in data["rule_sets"].items()
    }
    providers = {}
    for key, raw in data["providers"].items():
        providers[key] = ProviderSpec(
            key=key,
            display=raw["display"],
            enablement=raw["enablement"],
            enablement_detail=raw["enablement_detail"],
            action=raw["action"],
            threshold_rules=rule_sets.get(raw["rule_set"], ()) if raw["rule_set"] else (),
            default_min_tokens=raw["default_min_tokens"],
            default_min_note=raw["default_min_note"],
            increment_tokens=raw["increment_tokens"],
            ttl_summary=raw["ttl_summary"],
            ttl_seconds=raw["ttl_seconds"],
            read_discount=raw["read_discount"],
            write_cost=raw["write_cost"],
            verify_field=raw["verify_field"],
            routing=raw["routing"],
            batch_note=raw["batch_note"],
            gotchas=tuple(raw["gotchas"]),
            sources=tuple(raw["sources"]),
        )
    return {"aliases": data["aliases"], "rule_sets": rule_sets,
            "providers": providers, "meta": data["meta"]}


_KB = _load_kb()
PROVIDERS: dict = _KB["providers"]
_RULE_SETS: dict = _KB["rule_sets"]
_ALIASES: dict = _KB["aliases"]
META = {"tool": "cachelint",
        "knowledge_base": _KB["meta"]["knowledge_base"],
        "caveat": _KB["meta"]["caveat"]}


def norm_model(name: str) -> str:
    """Normalize a model name for pattern matching: lowercase, unify separators."""
    return re.sub(r"[ ._/]+", "-", (name or "").strip().lower())


def _detect_upstream(normalized: str) -> Optional[str]:
    """For OpenRouter model strings like 'anthropic/claude-opus-4.8'."""
    if re.search(r"claude|anthropic", normalized):
        return "anthropic"
    if re.search(r"gpt|openai|o[134](-|$)", normalized):
        return "openai"
    if re.search(r"gemini|google", normalized):
        return "gemini"
    if re.search(r"grok|x-ai|xai", normalized):
        return "xai"
    return None


def _match_rules(rules, normalized: str):
    for rule in rules:
        if re.search(rule.pattern, normalized):
            return rule
    return None


def resolve(provider: str, model: str) -> dict:
    """Resolve provider + model into a spec + effective minimum tokens.

    Returns a dict:
      spec (ProviderSpec), provider_key, model, min_tokens (int|None),
      tier_note (str), warnings (list[str]), upstream (str|None)
    """
    norm_provider = re.sub(r"[ ._/]+", "-", (provider or "").strip().lower())
    key = _ALIASES.get(norm_provider)
    if key is None:
        raise ValueError(
            f"Unknown provider '{provider}'. Known: "
            + ", ".join(sorted(set(_ALIASES.values())))
        )
    spec = PROVIDERS[key]
    normalized = norm_model(model)
    warnings: list[str] = []
    upstream = None

    if key == "openrouter":
        upstream = _detect_upstream(normalized)
        if upstream == "anthropic":
            rule = _match_rules(_RULE_SETS["anthropic"], normalized)
            min_tokens = rule.min_tokens if rule else PROVIDERS["anthropic"].default_min_tokens
            tier_note = (
                f"inherited from Anthropic upstream: {rule.note}" if rule
                else "inherited from Anthropic upstream (unknown model — conservative 4,096)"
            )
            warnings.append(
                "Threshold assumes routing to the DIRECT Anthropic API; if OpenRouter routes to "
                "a Bedrock host the floor may differ. Pin the provider."
            )
        elif upstream == "openai":
            min_tokens, tier_note = 1024, "inherited from OpenAI upstream (uniform 1,024)"
        elif upstream == "gemini":
            rule = _match_rules(_RULE_SETS["gemini"], normalized)
            min_tokens = rule.min_tokens if rule else PROVIDERS["gemini"].default_min_tokens
            tier_note = f"inherited from Gemini upstream: {rule.note}" if rule else \
                "inherited from Gemini upstream (conservative 4,096)"
        elif upstream == "xai":
            min_tokens, tier_note = None, "inherited from xAI upstream (no published threshold)"
        else:
            min_tokens, tier_note = None, "could not detect upstream provider from model name"
            warnings.append("Unknown upstream — threshold/enablement rules could not be inherited.")
        return {
            "spec": spec, "provider_key": key, "model": model,
            "min_tokens": min_tokens, "tier_note": tier_note,
            "warnings": warnings, "upstream": upstream,
        }

    if key == "vertex" and "claude" in normalized:
        rule = _match_rules(_RULE_SETS["anthropic"], normalized)
        min_tokens = rule.min_tokens if rule else PROVIDERS["anthropic"].default_min_tokens
        tier_note = (
            f"Claude-on-Vertex (Anthropic tiers): {rule.note}" if rule
            else "Claude-on-Vertex, unknown model — conservative 4,096"
        )
        return {
            "spec": spec, "provider_key": key, "model": model,
            "min_tokens": min_tokens, "tier_note": tier_note,
            "warnings": warnings, "upstream": "anthropic",
        }

    rule = _match_rules(spec.threshold_rules, normalized)
    if rule is not None:
        min_tokens, tier_note = rule.min_tokens, rule.note
    else:
        min_tokens, tier_note = spec.default_min_tokens, spec.default_min_note
        if spec.threshold_rules:
            warnings.append(
                f"Model '{model}' not recognized for {spec.display} — using the fallback "
                f"threshold ({min_tokens})."
            )

    return {
        "spec": spec, "provider_key": key, "model": model,
        "min_tokens": min_tokens, "tier_note": tier_note,
        "warnings": warnings, "upstream": upstream,
    }
