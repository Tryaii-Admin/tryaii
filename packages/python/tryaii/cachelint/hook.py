"""SDK warn hook: pre-flight cache lint + runtime cache verification.

This is the SDK's seat for the ACTUAL rendered outgoing prompt — post
routing, post message-assembly, post truncation — and for the response
`usage` fields. The future AST-introspection phase extends this class.

Behavior (see docs/sdk/client/cache-lint.md):
  * warn-only and fail-open: no method here ever raises — a lint failure
    must never break or block the user's API call;
  * problems only: warnings fire for BELOW_THRESHOLD, EFFECTIVELY_UNCACHEABLE,
    and UNKNOWN_THRESHOLD-with-blockers verdicts — never for healthy prompts;
  * once per prompt shape: each unique (model, canonical prompt) pair warns at
    most once per client instance (FIFO-capped memory, stale entries reset);
  * verification: from the SECOND call with the same shape onward, a prompt
    the lint predicted cacheable that reports 0 cached tokens in `usage`
    triggers a single VERIFY_MISS warning for that shape.

Output goes to stderr by default: the package root installs a NullHandler,
so `logger.warning` would be invisible — and a user who explicitly opted in
with cache_lint="warn" has asked for visible output. This is deliberately
the only library-level stderr writer outside the CLI, and it only ever runs
under that explicit opt-in. Each warning is mirrored to logger.debug for
apps with logging configured.

Message formats are byte-identical to the Node SDK's hook (pinned by
mirrored unit tests in test_cachelint_hook.py / cachelint-hook.test.ts).
"""

from __future__ import annotations

import hashlib
import logging
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger("tryaii.cachelint.hook")

_PREFIX = "[tryaii cachelint]"
_MAX_SHAPES = 1024          # FIFO cap on remembered prompt shapes
_STALE_AFTER_S = 3600.0     # idle window after which a shape's call counter resets

_NOTE_LINE = (
    f"{_PREFIX} note: reported once per unique prompt shape per client; "
    'set cache_lint="off" or TRYAII_CACHE_LINT=off to disable'
)
_TIKTOKEN_HINT = (
    f"{_PREFIX} note: cache lint skipped for an OpenAI/xAI-routed prompt — "
    "install tryaii[cachelint] to enable exact token analysis"
)

# Verdicts that warn on preflight. UNKNOWN_THRESHOLD additionally requires a
# blocking finding: bare UNKNOWN is a knowledge-base gap (e.g. a deepseek
# slug), not a problem in the user's prompt.
_WARN_VERDICTS = ("BELOW_THRESHOLD", "EFFECTIVELY_UNCACHEABLE", "UNKNOWN_THRESHOLD")

# Verdicts under which a cache read is genuinely expected on repeat calls.
# CACHEABLE_WITH_ACTION is excluded: this SDK never sends cache_control, so
# zero cached tokens is the CORRECT outcome there, not a mismatch.
_EXPECT_CACHE_VERDICTS = ("CACHEABLE", "CACHEABLE_PREFIX")


def _stderr_sink(line: str) -> None:
    # Resolved at call time so monkeypatched/captured stderr is honored.
    sys.stderr.write(line + "\n")


@dataclass
class _Prediction:
    model: str
    verdict: str
    stable_tokens: int
    min_tokens: Optional[int]
    expect_cache: bool
    calls: int
    verify_warned: bool
    last_seen: float


class CacheLintHook:
    """Per-client-instance lint state. Public methods never raise."""

    def __init__(
        self,
        sink: Optional[Callable[[str], None]] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._sink = sink or _stderr_sink
        self._clock = clock
        self._seen: dict[str, _Prediction] = {}
        self._note_shown = False
        self._tiktoken_hint_shown = False

    # -- internals ------------------------------------------------------

    def _emit(self, line: str) -> None:
        try:
            self._sink(line)
        except Exception:  # noqa: BLE001 -- a broken sink must not break the call
            pass
        logger.debug("%s", line)

    @staticmethod
    def _key(openrouter_model: str, canonical: str) -> str:
        raw = (openrouter_model + "\x00" + canonical).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:16]

    # -- public API -----------------------------------------------------

    def preflight(self, openrouter_model: str, messages: list) -> Optional[str]:
        """Lint the outgoing request; returns the shape key (or None on failure)."""
        try:
            return self._preflight(openrouter_model, messages)
        except ImportError:
            if not self._tiktoken_hint_shown:
                self._tiktoken_hint_shown = True
                self._emit(_TIKTOKEN_HINT)
            return None
        except Exception:  # noqa: BLE001 -- fail-open: never break the user's call
            logger.debug("cachelint preflight error", exc_info=True)
            return None

    def _preflight(self, openrouter_model: str, messages: list) -> Optional[str]:
        from tryaii.cachelint import analyze, build_canonical

        canonical, _, _ = build_canonical({"messages": messages})
        key = self._key(openrouter_model, canonical)
        now = self._clock()

        rec = self._seen.get(key)
        if rec is not None:
            if now - rec.last_seen > _STALE_AFTER_S:
                rec.calls = 0
            rec.calls += 1
            rec.last_seen = now
            return key

        if len(self._seen) >= _MAX_SHAPES:
            self._seen.pop(next(iter(self._seen)))

        result = analyze({
            "prompt": {"messages": messages},
            "llm": {"provider": "openrouter", "name": openrouter_model},
        })
        item = result["items"][0]
        verdict = item["verdict"]
        limited_by = item["stable_prefix"]["limited_by"]

        self._seen[key] = _Prediction(
            model=openrouter_model,
            verdict=verdict["code"],
            stable_tokens=item["stable_prefix"]["tokens"],
            min_tokens=item["threshold"]["min_tokens"],
            expect_cache=verdict["code"] in _EXPECT_CACHE_VERDICTS,
            calls=1,
            verify_warned=False,
            last_seen=now,
        )

        triggered = verdict["code"] in _WARN_VERDICTS and (
            verdict["code"] != "UNKNOWN_THRESHOLD" or limited_by is not None
        )
        if triggered:
            self._emit(f"{_PREFIX} {verdict['code']} for {openrouter_model}: "
                       f"{verdict['summary']}")
            if limited_by is not None:
                self._emit(f"{_PREFIX}   first blocker: {limited_by['kind']} at "
                           f"{limited_by['section']}+{limited_by['section_offset']} "
                           f"({int(limited_by['pct_into_prompt'])}% in)")
            if not self._note_shown:
                self._note_shown = True
                self._emit(_NOTE_LINE)
        return key

    def verify(self, key: Optional[str], usage) -> None:
        """Compare the preflight prediction against the response usage fields."""
        try:
            self._verify(key, usage)
        except Exception:  # noqa: BLE001 -- fail-open: never break the user's call
            logger.debug("cachelint verify error", exc_info=True)

    def _verify(self, key: Optional[str], usage) -> None:
        if key is None or not isinstance(usage, dict):
            return
        rec = self._seen.get(key)
        if rec is None or rec.calls < 2 or not rec.expect_cache or rec.verify_warned:
            return

        cached = None
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict) and isinstance(details.get("cached_tokens"), (int, float)) \
                and not isinstance(details.get("cached_tokens"), bool):
            cached = details["cached_tokens"]
        elif isinstance(usage.get("cached_tokens"), (int, float)) \
                and not isinstance(usage.get("cached_tokens"), bool):
            cached = usage["cached_tokens"]

        discount = usage.get("cache_discount")
        if not isinstance(discount, (int, float)) or isinstance(discount, bool):
            discount = None

        if cached is None and discount is None:
            return  # unverifiable — stay silent
        if (cached or 0) == 0 and (discount or 0) == 0:
            rec.verify_warned = True
            self._emit(
                f"{_PREFIX} VERIFY_MISS for {rec.model}: predicted ~{rec.stable_tokens} tok "
                f"cacheable prefix, but usage reports 0 cached tokens on repeat call "
                f"#{rec.calls} of this prompt shape — a silent invalidator or missing "
                f"provider support may be the cause"
            )
