"""Cost of a Claude Code call at API list prices, from TryAii's model catalog.

The rule (shared/monitor/SPEC.md §10.3):

- the model id in the log (``claude-opus-5-5``) is resolved to a catalog id
  (``anthropic/claude-opus-5.5``) with the same resolver the monitor uses for
  its anchor (exact -> slug -> normalized -> alias). The catalog is the one
  the CLI selects: the signed full catalog when it is cached after
  ``tryaii login``, else the packaged starter;
- input and output prices come from the catalog. Only when the catalog does
  not price the model, ``FALLBACK_LIST_PRICES`` (Anthropic's published list
  prices for the Claude models the catalogs miss) is used and the model is
  reported as "list price (fallback)". A model in neither is **unpriced**:
  its cost is ``None`` and it is reported as such, never guessed;
- ``input_tokens`` (Anthropic's uncached input) x the input price;
- cache writes: the 5-minute part x 1.25 x the input price, the 1-hour part
  x 2 x the input price. A write without a lifetime breakdown, or the part
  of the total the breakdown does not cover, is priced as 5-minute;
- cache reads x the catalog's cache-read price when the model's catalog
  entry has one (``pricing.cache_read_per_1k``), else x the model's
  multiplier in ``CACHE_READ_MULTIPLIERS`` x the input price (0.1 for a
  model not in the table);
- ``output_tokens`` (thinking included, as Anthropic bills it) x the output
  price;
- a fallback model with a long-context tier (``claude-haiku-5-5``) uses the
  higher prices when the call's prompt (input + cache write + cache read)
  exceeds the tier's threshold.

``CACHE_READ_MULTIPLIERS`` and ``FALLBACK_LIST_PRICES`` are Anthropic's
published rates (pricing reference cached 2026-10-06). They are a stopgap:
both tables go away once the catalog carries cache prices and these models
(the cache-price-sync work under TS-227 in Tryaii-BT).

Every figure is an estimate at API list prices: a Claude subscription does
not bill per token.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Optional

from tryaii.claude_code.reader import Call

CACHE_WRITE_5M_FACTOR = 1.25
CACHE_WRITE_1H_FACTOR = 2.0
#: Cache read as a share of the input price when the catalog has no cache
#: price (Anthropic list prices, 2026-10-06). Keyed by the native model id.
CACHE_READ_MULTIPLIERS = {
    "claude-opus-5-5": 0.05,     # $0.20 / MTok on $4 input
    "claude-fable-5-1": 0.025,   # $0.25 / MTok on $10 input
    "claude-mythos-5-1": 0.025,  # $0.25 / MTok on $10 input
}
DEFAULT_CACHE_READ_FACTOR = 0.1
#: USD per million tokens for Claude models the catalogs do not price yet:
#: (input, output) and an optional long-context tier
#: (prompt-token threshold, input, output).
FALLBACK_LIST_PRICES = {
    # claude-opus-5 left this table once both catalogs carried it (the full
    # catalog since the 2026-10-01 snapshot, the starter since 2026.10.23.1).
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50,
                         "long_context": (100_000, 0.50, 2.50)},
}
CACHE_READ_KEYS = ("cache_read_per_1k", "input_cache_read_per_1k")
COST_PARTS = ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output")
SOURCE_CATALOG = "catalog"
SOURCE_FALLBACK = "list price (fallback)"


@dataclass(frozen=True)
class Price:
    """USD per token. ``source`` is ``catalog`` or ``list price (fallback)``;
    ``cache_read_source`` is ``catalog`` or ``<multiplier>x input``."""

    catalog_id: Optional[str]
    input: float
    output: float
    cache_read_factor: Optional[float]  # None = cache_read_per_token is from the catalog
    cache_read_per_token: Optional[float]
    source: str
    long_context: Optional[tuple] = None  # (threshold, input/token, output/token)

    @property
    def cache_read(self) -> float:
        return self.cache_read_for(self.input)

    def cache_read_for(self, input_price: float) -> float:
        if self.cache_read_factor is None:
            return self.cache_read_per_token
        return input_price * self.cache_read_factor

    @property
    def cache_read_source(self) -> str:
        if self.cache_read_factor is None:
            return SOURCE_CATALOG
        return f"{self.cache_read_factor:g}x input"


def _finite(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def native_id(model_id: str) -> str:
    """``anthropic/claude-opus-5.5`` -> ``claude-opus-5-5`` (the tables' key)."""
    name = model_id.split("/", 1)[1] if model_id.startswith("anthropic/") else model_id
    return name.replace(".", "-")


def cache_read_factor(model: str) -> float:
    return CACHE_READ_MULTIPLIERS.get(native_id(model), DEFAULT_CACHE_READ_FACTOR)


class Pricer:
    """Prices calls with one catalog bundle (memoised per model id)."""

    def __init__(self, bundle, registry=None):
        from tryaii.registry.models import ModelRegistry

        self.bundle = bundle
        self.registry = registry or ModelRegistry.from_bundle(bundle)
        self._raw = {}
        for entry in getattr(bundle, "model_entries", lambda: [])() or []:
            if isinstance(entry, dict) and isinstance(entry.get("model_id"), str):
                self._raw[entry["model_id"]] = entry.get("pricing") or {}
        self._memo: dict = {}

    @property
    def catalog_version(self) -> Optional[str]:
        return getattr(self.bundle, "version", None)

    @property
    def catalog_kind(self) -> Optional[str]:
        try:
            return getattr(self.bundle, "kind", None)
        except Exception:  # noqa: BLE001
            return None

    def resolve(self, model: str) -> Optional[str]:
        from tryaii.monitor.winner import resolve_anchor

        try:
            return resolve_anchor(model, self.registry)
        except Exception:  # noqa: BLE001 - an odd id is just unknown
            return None

    def _catalog_price(self, model: str) -> Optional[Price]:
        catalog_id = self.resolve(model)
        info = self.registry.get_model(catalog_id) if catalog_id else None
        pricing = getattr(info, "pricing", None)
        if pricing is None:
            return None
        inp = _finite(pricing.input_per_1k)
        out = _finite(pricing.output_per_1k)
        if inp is None or out is None:
            return None
        raw = self._raw.get(catalog_id) or {}
        read = None
        for key in CACHE_READ_KEYS:
            read = _finite(raw.get(key)) if isinstance(raw, dict) else None
            if read is not None:
                break
        return Price(catalog_id=catalog_id, input=inp / 1000, output=out / 1000,
                     cache_read_factor=None if read is not None else cache_read_factor(model),
                     cache_read_per_token=read / 1000 if read is not None else None,
                     source=SOURCE_CATALOG)

    @staticmethod
    def _fallback_price(model: str) -> Optional[Price]:
        entry = FALLBACK_LIST_PRICES.get(native_id(model))
        if entry is None:
            return None
        long_context = None
        if entry.get("long_context"):
            threshold, inp, out = entry["long_context"]
            long_context = (threshold, inp / 1e6, out / 1e6)
        return Price(catalog_id=None, input=entry["input"] / 1e6, output=entry["output"] / 1e6,
                     cache_read_factor=cache_read_factor(model), cache_read_per_token=None,
                     source=SOURCE_FALLBACK, long_context=long_context)

    def price(self, model: str) -> Optional[Price]:
        if model not in self._memo:
            self._memo[model] = self._catalog_price(model) or self._fallback_price(model)
        return self._memo[model]

    def cost(self, call: Call, model: Optional[str] = None) -> Optional[dict]:
        """``{"input", "cache_write_5m", "cache_write_1h", "cache_read",
        "output", "total"}`` in USD (unrounded), or None when unpriced.
        ``model`` prices the same usage at another model (what-if)."""
        price = self.price(model or call.model)
        if price is None:
            return None
        inp, out = price.input, price.output
        if price.long_context and call.context_tokens > price.long_context[0]:
            inp, out = price.long_context[1], price.long_context[2]
        w1 = call.tokens("cache_write_1h_tokens")
        w5 = max(call.cache_write_tokens - w1, 0)
        parts = {
            "input": call.tokens("input_tokens") * inp,
            "cache_write_5m": w5 * inp * CACHE_WRITE_5M_FACTOR,
            "cache_write_1h": w1 * inp * CACHE_WRITE_1H_FACTOR,
            "cache_read": call.tokens("cache_read_input_tokens") * price.cache_read_for(inp),
            "output": call.tokens("output_tokens") * out,
        }
        parts["total"] = sum(parts[k] for k in COST_PARTS)
        return parts


def claude_code_model(model_id) -> bool:
    """True for a catalog id Claude Code can be set to: not a ``:<variant>``
    of another id. The catalog lists variants of a model under the same
    display name (``anthropic/claude-opus-4.7:batch`` the Batch API,
    ``:free`` / ``:thinking`` / ``:extended`` router offerings), and Claude
    Code's ``/model`` or a subagent's ``model:`` reaches none of them, so
    Claude Code advice never names one."""
    return isinstance(model_id, str) and bool(model_id) and ":" not in model_id


def _version_key(model_id: str) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", model_id))


def cheapest_tier_model(pricer: Pricer, family: str = "haiku") -> Optional[str]:
    """The newest priced model of a Claude family (``haiku``) in the catalog or
    the fallback table: what ``model: haiku`` in a subagent definition gets.
    None when neither has one."""
    best = None
    candidates = [m for m in pricer.registry.model_ids if m.startswith("anthropic/")]
    candidates += list(FALLBACK_LIST_PRICES)
    for model_id in candidates:
        if f"claude-{family}" not in model_id or not claude_code_model(model_id):
            continue
        if pricer.price(model_id) is None:
            continue
        if best is None or _version_key(model_id) > _version_key(best):
            best = model_id
    return best
