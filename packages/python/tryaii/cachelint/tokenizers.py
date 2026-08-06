"""Token counting per provider — exact-required policy (SPEC.md §3).

- OpenAI / xAI / *-with-openai-upstream: exact via tiktoken (o200k_base).
  tiktoken is a REQUIRED extra for these paths — a missing install raises
  with instructions instead of silently degrading (SPEC.md delta o).
- Anthropic: heuristic estimate (~3.5 chars/token English). tiktoken is WRONG
  for Claude (undercounts 15-20%+) — for exact counts use the official
  POST /v1/messages/count_tokens endpoint.
- Gemini / Vertex / Bedrock / others: heuristic estimate; each has a
  countTokens-style API for exact numbers.

Every result is labeled with its method so downstream verdicts can hedge.
Lengths are Unicode code points (SPEC.md §1.1); rounding is half-even
(SPEC.md §1.2); the method label reports the rate actually used after the
non-ASCII adjustment (SPEC.md delta k).
"""

from __future__ import annotations

from dataclasses import dataclass

_TIKTOKEN_ENC = None

_TIKTOKEN_INSTALL_MSG = (
    "tiktoken is required for cachelint. Install with: pip install tryaii[cachelint]"
)


def _get_tiktoken():
    global _TIKTOKEN_ENC
    if _TIKTOKEN_ENC is None:
        try:
            import tiktoken
        except ImportError as exc:                       # SPEC.md delta o: hard error
            raise ImportError(_TIKTOKEN_INSTALL_MSG) from exc
        _TIKTOKEN_ENC = tiktoken.get_encoding("o200k_base")
    return _TIKTOKEN_ENC


@dataclass(frozen=True)
class TokenCount:
    tokens: int
    method: str      # "tiktoken(o200k_base)" | "estimate(~N chars/tok)" | "empty"
    exact: bool
    note: str = ""

    def to_dict(self) -> dict:
        return {"tokens": self.tokens, "method": self.method, "exact": self.exact,
                "note": self.note}


# chars-per-token rates for the heuristic path (latin-heavy text)
_RATES = {
    "anthropic": 3.5,
    "gemini": 4.0,
    "vertex": 4.0,
    "openai": 4.0,
    "xai": 3.8,
    "openrouter": 4.0,
    "bedrock": 3.7,
}

_EXACT_NOTES = {
    "anthropic": "for exact counts use POST /v1/messages/count_tokens (never tiktoken for Claude)",
    "gemini": "for exact counts use the Gemini countTokens API",
    "vertex": "for exact counts use the Vertex countTokens API / Anthropic count_tokens",
    "bedrock": "for exact counts use the underlying vendor's token-counting API",
    "xai": "xAI tokenizer approximated with o200k_base",
    "openrouter": "count depends on the upstream model's tokenizer",
    "openai": "",
}

# Providers whose tokenizer tiktoken o200k_base approximates well enough to call exact-ish.
_TIKTOKEN_OK = {"openai"}
_TIKTOKEN_APPROX = {"xai"}   # close, but not official — label as approximate

_NON_ASCII_SWITCH = 0.3
_NON_ASCII_RATE = 2.6


def _estimate(text: str, rate: float) -> tuple[int, float]:
    """Heuristic count; returns (tokens, effective_rate) — SPEC.md delta k."""
    if not text:
        return 0, rate
    # Non-latin scripts (Hebrew, CJK, ...) tokenize denser per char.
    non_ascii = sum(1 for c in text if ord(c) > 127)
    if non_ascii / max(1, len(text)) > _NON_ASCII_SWITCH:
        rate = min(rate, _NON_ASCII_RATE)
    return max(1, round(len(text) / rate)), rate


def count_tokens(text: str, provider_key: str, upstream: str | None = None) -> TokenCount:
    """Count/estimate tokens for `text` under the given provider.

    `upstream` (for openrouter) refines the choice of tokenizer.
    """
    key = provider_key
    effective = upstream or key
    if not text:
        return TokenCount(0, "empty", True)

    if effective in _TIKTOKEN_OK or effective in _TIKTOKEN_APPROX:
        enc = _get_tiktoken()
        # Special tokens encode as ordinary text (mirrors js-tiktoken encode(text, [], [])).
        n = len(enc.encode(text, disallowed_special=()))
        exact = effective in _TIKTOKEN_OK
        note = "" if exact else _EXACT_NOTES.get(effective, "approximated with o200k_base")
        return TokenCount(n, "tiktoken(o200k_base)", exact, note)

    rate = _RATES.get(effective, _RATES.get(key, 4.0))
    n, used_rate = _estimate(text, rate)
    return TokenCount(
        n,
        f"estimate(~{used_rate:g} chars/tok)",
        False,
        _EXACT_NOTES.get(effective, _EXACT_NOTES.get(key, "")) or "heuristic estimate (+/-20%)",
    )
