"""
cachelint — prompt-cache pre-flight analyzer.

Analyzes prompts BEFORE they are sent: 18 dynamic-content detectors, a
7-provider caching knowledge base (thresholds, TTLs, enablement), stable-
prefix computation, verdicts, and sequence analysis (predicted HIT / PARTIAL
/ MISS across consecutive requests).

Usage:
    from tryaii.cachelint import analyze, render_report

    result = analyze({
        "prompt": {"system": "...", "messages": [{"role": "user", "content": "..."}]},
        "llm": {"provider": "anthropic", "name": "claude-fable-5"},
    })
    print(result["items"][0]["verdict"])
    print(render_report(result))

Exact token counts for OpenAI/xAI paths require the `cachelint` extra:
    pip install tryaii[cachelint]

Behavior contract: shared/cachelint/SPEC.md (mirrored by the Node SDK's
`tryaii/cachelint` module; both conform to the same golden fixtures).
"""

from tryaii.cachelint.analyzer import analyze_item, build_canonical
from tryaii.cachelint.api import analyze, analyze_full
from tryaii.cachelint.providers import resolve
from tryaii.cachelint.report import render as render_report

__all__ = [
    "analyze",
    "analyze_full",
    "analyze_item",
    "build_canonical",
    "render_report",
    "resolve",
]
