"""
Dynamic model scoring engine -- ``satisficing-v1``.

Routing is a *satisficing* decision, not a weighted average:

1. Quality (``q'``) is a catalog-normalised benchmark aggregate in [0, 1]:
   a weighted mean over the 5 benchmarks most similar to the prompt, where each
   term's weight is ``sim(b) * BENCHMARK_WEIGHTS[b] * coverage(b)`` and is
   halved again (``IMPUTED_TERM_WEIGHT``) when that model's score for ``b`` had
   to be imputed -- so neither a benchmark almost nobody reports nor an invented
   value can dominate the decision.
2. A **band** of acceptable quality is opened below the best candidate,
   ``eps = EPS_UNIT * ((cost - 1) + (speed - 1)) / quality``. At priority
   (5,1,1) the band is empty, so routing is strict quality.
3. Inside the band, models are ranked purely on a weighted average of the
   **cost** and **speed** utilities -- both log-scaled, so "10x cheaper" and
   "half the wait" are constant-sized improvements anywhere on the scale.

The old three-term weighted average (``quality*qW + cost*cW + speed*sW``) and
the 0.1..0.95 per-call min-max rescale are both gone: the rescale made every
score incomparable between calls, and the linear-in-dollars cost term could not
tell $0.10/M from $0.50/M.

``EPS_UNIT`` is THE tuning dial of this algorithm -- see its definition below.
This module must stay behaviour-identical with
``packages/node/src/scoring/engine.ts``, down to the reasoning string.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from tryaii.registry.models import ModelInfo, compute_benchmark_coverage
from tryaii.scoring.benchmarks import BenchmarkNormalizer
from tryaii.scoring.priorities import DEFAULT_PRIORITIES, Priorities


@dataclass
class ModelScore:
    """Detailed score breakdown for a single model.

    ``final_score`` is comparable **within one routing call only** -- the band
    edge and ``quality_best`` are properties of the call, not of the model. The
    globally comparable per-model numbers are ``quality_score`` (catalog-
    normalised), ``cost_score`` and ``speed_score``.

    Invariant, to 1e-6 before the 4-decimal rounding::

        quality_contribution + cost_contribution + speed_contribution
            + band_base == final_score
    """

    model_id: str
    final_score: float  # 0-1; >= 0.5 iff the model cleared the quality band
    quality_score: float  # q' in [0,1], catalog-normalised, imputation-shrunk
    cost_score: float  # U_c in [0,1] (higher = cheaper)
    speed_score: float  # U_s in [0,1] (higher = faster)
    quality_contribution: float
    cost_contribution: float
    speed_contribution: float
    top_benchmarks: list[tuple[str, float]]  # real (non-imputed) scored benchmarks
    reasoning: str  # Human-readable explanation
    # --- added by satisficing-v1; defaulted so older wire shapes still load.
    band_base: float = 0.0  # 0.5 when in band, else 0.0
    in_band: bool = False
    quality_tolerance: float = 0.0  # eps actually used for this call
    quality_best: float = 0.0  # q'_best over this call's candidates
    signal_flags: list[str] = field(default_factory=list)
    # Effective per-term quality weights actually used for q' -- i.e.
    # ``similarity * BENCHMARK_WEIGHTS[b] * coverage(b)**COVERAGE_EXPONENT``,
    # times ``IMPUTED_TERM_WEIGHT`` for a term this model had imputed -- keyed
    # by benchmark name, rounded to SCORE_DECIMALS. Diagnostic only: it is not
    # part of any wire payload.
    benchmark_weights: dict[str, float] = field(default_factory=dict)
    # Unrounded parts, for callers that need the full precision the rounded
    # public fields above drop.
    q_prime: float = 0.0
    u_cost: float = 0.0
    u_speed: float = 0.0
    t300: Optional[float] = None  # None when throughput is unknown


# ---------------------------------------------------------------------------
# Cost utility
# ---------------------------------------------------------------------------

# $/M anchors: P_LO -> U_c = 1, P_HI -> U_c = 0. Three decades, so the
# denominator is ln(1000) = 6.907755 and every 10x cheaper is +0.3333 U_c.
P_LO = 0.05
P_HI = 50.0
_LN_COST_SPAN = math.log(P_HI) - math.log(P_LO)

# Seconds-to-300-tokens anchors: T_LO -> U_s = 1, T_HI -> U_s = 0. The
# denominator is ln(100) = 4.605170, so halving the wait is +0.1505 U_s.
T_LO = 0.3
T_HI = 30.0
_LN_SPEED_SPAN = math.log(T_HI) - math.log(T_LO)

# The answer length the speed utility is defined against.
SPEED_TARGET_TOKENS = 300.0

# Flags surfaced on ``ModelScore.signal_flags`` and in the reasoning string.
COST_UNKNOWN_FLAG = "cost: unknown"
SPEED_UNKNOWN_FLAG = "speed: unknown"
TTFT_ESTIMATED_FLAG = "ttft: estimated"
NO_SIGNAL_FLAG = "no benchmark signal"

# Utilities are quantised to this many decimals *before* they are compared, so
# measurement noise in a 5-row tps/ttft sample cannot reorder near-ties inside
# the band. 3 decimals of U_s is a resolution of ~0.7% in seconds -- far below
# the noise. Both SDKs round half-to-even here.
UTILITY_DECIMALS = 3

# Decimals of the public ``final_score`` and the part fields.
SCORE_DECIMALS = 4


def _clamp01(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def cost_utility(price_per_1k: Optional[float]) -> tuple[float, Optional[str]]:
    """Log-scaled cost utility from the average $/1k token price.

    ``price_per_1k`` is ``(input_per_1k + output_per_1k) / 2`` -- the 50/50
    blend is kept for parity with the previous engine. Returns ``(U_c, flag)``.

    A missing or non-finite price scores **0.0, not neutral**: a model whose
    price we do not know must never win a cost comparison.
    """
    if price_per_1k is None or not math.isfinite(price_per_1k):
        return 0.0, COST_UNKNOWN_FLAG
    price_per_m = price_per_1k * 1000.0
    if price_per_m <= 0:
        return 1.0, None
    utility = (math.log(P_HI) - math.log(price_per_m)) / _LN_COST_SPAN
    return _clamp01(utility), None


def price_per_million(model: ModelInfo) -> Optional[float]:
    """Average $/1M tokens for a model, or None when pricing is unknown."""
    if not model.pricing:
        return None
    avg = (model.pricing.input_per_1k + model.pricing.output_per_1k) / 2
    if not math.isfinite(avg):
        return None
    return avg * 1000.0


# ---------------------------------------------------------------------------
# Speed utility
# ---------------------------------------------------------------------------


def t300_seconds(tps: float, ttft_ms: float) -> float:
    """Seconds to a 300-token answer: time to first token plus generation."""
    return ttft_ms / 1000.0 + SPEED_TARGET_TOKENS / tps


def speed_utility_from_t300(t300: float) -> float:
    """U_s from a T300 in seconds (log-scaled between T_LO and T_HI)."""
    if t300 <= 0:
        return 1.0
    return _clamp01((math.log(T_HI) - math.log(t300)) / _LN_SPEED_SPAN)


# Used only when the candidate set carries no speed measurements at all (an
# empty or hand-built registry), so the fallbacks always have a definition. The
# shipped catalog's own runtime values are ~754 ms and ~0.2423.
FALLBACK_MEDIAN_TTFT_MS = 800.0
FALLBACK_P25_SPEED_UTILITY = 0.242


@dataclass(frozen=True)
class RegistryStats:
    """Catalog-derived fallbacks for models with incomplete speed data.

    Computed at runtime from the loaded registry (there is no generated table),
    identically in both SDKs -- see :func:`registry_speed_stats`.
    """

    median_ttft_ms: float
    p25_speed_utility: float


def _median(sorted_values: list[float]) -> float:
    count = len(sorted_values)
    mid = count // 2
    if count % 2 == 1:
        return sorted_values[mid]
    return (sorted_values[mid - 1] + sorted_values[mid]) / 2


def _percentile_linear(sorted_values: list[float], fraction: float) -> float:
    """Linear-interpolation percentile, matching ``numpy.percentile``."""
    count = len(sorted_values)
    if count == 1:
        return sorted_values[0]
    position = fraction * (count - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[int(position)]
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def _has_ttft(model: ModelInfo) -> bool:
    return (
        model.ttft_ms is not None
        and math.isfinite(model.ttft_ms)
        and model.ttft_ms > 0
    )


def _has_tps(model: ModelInfo) -> bool:
    return (
        model.tokens_per_second is not None
        and math.isfinite(model.tokens_per_second)
        and model.tokens_per_second > 0
    )


def registry_speed_stats(models: list[ModelInfo]) -> RegistryStats:
    """Median TTFT and the p25 of U_s over the models that carry measurements.

    ``median_ttft_ms`` stands in for a model that has a throughput measurement
    but no TTFT. ``p25_speed_utility`` is the U_s handed to a model with no
    throughput at all -- deliberately pessimistic (below the median of the same
    population), so an unmeasured model can never win a speed-led route.

    Computed from the candidate set handed to ``score_models`` (so a filtered
    route uses the filtered population), identically in both SDKs.
    """
    ttfts = sorted(m.ttft_ms for m in models if _has_ttft(m))
    utilities = sorted(
        speed_utility_from_t300(t300_seconds(m.tokens_per_second, m.ttft_ms))
        for m in models
        if _has_tps(m) and _has_ttft(m)
    )
    return RegistryStats(
        median_ttft_ms=_median(ttfts) if ttfts else FALLBACK_MEDIAN_TTFT_MS,
        p25_speed_utility=(
            _percentile_linear(utilities, 0.25)
            if utilities
            else FALLBACK_P25_SPEED_UTILITY
        ),
    )


def _speed_parts(
    model: ModelInfo, registry_stats: RegistryStats
) -> tuple[float, Optional[str], Optional[float]]:
    """``(U_s, flag, T300)``; T300 is None when throughput is unknown."""
    if not _has_tps(model):
        return registry_stats.p25_speed_utility, SPEED_UNKNOWN_FLAG, None
    tps = model.tokens_per_second
    ttft = model.ttft_ms
    flag: Optional[str] = None
    if not _has_ttft(model):
        ttft = registry_stats.median_ttft_ms
        flag = TTFT_ESTIMATED_FLAG
    t300 = t300_seconds(tps, ttft)
    return speed_utility_from_t300(t300), flag, t300


def speed_utility(
    model: ModelInfo, registry_stats: RegistryStats
) -> tuple[float, Optional[str]]:
    """Log-scaled speed utility for a model: ``(U_s, flag)``.

    A missing TTFT falls back to the catalog median TTFT (flagged
    ``ttft: estimated``); a missing throughput falls back to the catalog p25 of
    U_s without computing a T300 at all (flagged ``speed: unknown``).
    """
    utility, flag, _ = _speed_parts(model, registry_stats)
    return utility, flag


# ---------------------------------------------------------------------------
# The band
# ---------------------------------------------------------------------------

# THE tuning dial of satisficing-v1. The band is
# ``eps = EPS_UNIT * ((cost - 1) + (speed - 1)) / quality`` in q' units, so at
# (3,3,3) the router spends up to 0.136 of quality to get a cheaper or faster
# model. eps is linear in ``(c-1)+(s-1)`` (it grows smoothly, never stepwise)
# and inversely proportional to the quality priority.
#
# 0.102 is 1.6x the calibrated base tolerance, picked so eps at (3,3,3) is a
# large fraction of the typical quality spread among the strongest catalog
# models: wide enough that balanced routing genuinely trades a little quality
# for a much cheaper or faster model, narrow enough that it never drops to a
# clearly weaker tier. A wider dial buys price with quality, linearly and
# predictably. Raise it to spend more quality, lower it to spend less --
# nothing else in this module changes when it moves. Recalibrate when the
# catalog's q' spread moves (see docs/sdk/routing/scoring.md).
EPS_UNIT = 0.102


def quality_tolerance(priorities: Priorities, eps_unit: float = EPS_UNIT) -> float:
    """The band width ``eps``, in q' units, for a priority triple.

    Zero exactly when cost and speed are both priority 1 -- the same condition
    that switches the secondary term off, so "no band" and "no trade" always
    agree.
    """
    return (
        eps_unit * ((priorities.cost - 1) + (priorities.speed - 1)) / priorities.quality
    )


@dataclass(frozen=True)
class SatisficingCombination:
    """What :func:`satisficing_combine` returns.

    ``utility`` is the internal ordering key (never published); ``final_score``
    is the public number BEFORE the 4-decimal rounding; ``sec`` is None outside
    the band.
    """

    utility: float
    final_score: float
    sec: Optional[float]
    in_band: bool
    band_base: float
    quality_contribution: float
    cost_contribution: float
    speed_contribution: float


def satisficing_combine(
    q_prime: float,
    u_cost_q: float,
    u_speed_q: float,
    q_best: float,
    eps: float,
    wc: float,
    ws: float,
) -> SatisficingCombination:
    """The whole decision rule, as a pure function of seven numbers.

    ``u_cost_q`` / ``u_speed_q`` are the **3-decimal quantised** utilities (see
    ``UTILITY_DECIMALS``) -- quantise before calling, not after. Mirrors
    ``satisficingCombine`` in the Node SDK exactly.
    """
    in_band = q_prime >= q_best - eps
    w_total = wc + ws
    if w_total <= 0:
        # Strict quality: the secondary term is off, so final_score IS q' and
        # quality_contribution carries the whole score.
        return SatisficingCombination(
            utility=q_prime,
            final_score=q_prime,
            sec=None,
            in_band=in_band,
            band_base=0.0,
            quality_contribution=q_prime,
            cost_contribution=0.0,
            speed_contribution=0.0,
        )
    if in_band:
        sec = (wc * u_cost_q + ws * u_speed_q) / w_total
        return SatisficingCombination(
            utility=1.0 + sec,
            final_score=0.5 + 0.5 * sec,
            sec=sec,
            in_band=True,
            band_base=0.5,
            quality_contribution=0.0,
            cost_contribution=0.5 * wc * u_cost_q / w_total,
            speed_contribution=0.5 * ws * u_speed_q / w_total,
        )
    final = 0.5 * q_prime
    return SatisficingCombination(
        utility=q_prime,
        final_score=final,
        sec=None,
        in_band=False,
        band_base=0.0,
        quality_contribution=final,
        cost_contribution=0.0,
        speed_contribution=0.0,
    )


TOP_BENCHMARKS_FOR_SCORING = 5

# Neutral quality used only as a last-resort fallback when a prompt matches no
# benchmark at all (every similarity clamps to 0), so it stays routable on
# cost/speed instead of being dropped. See score_models' neutral_fallback retry.
NEUTRAL_QUALITY_SCORE = 0.5

# Weight floor applied ONLY on the all-no-signal fallback path, so cost/speed
# can still break ties there even when the user suppressed them. Applying it
# anywhere else would contaminate the (5,1,1) and (1,5,1) routing anchors.
NO_SIGNAL_WEIGHT_FLOOR = 0.1

# Shrinkage constant for imputing a missing benchmark. The imputed value blends
# the model's own demonstrated level with the registry median, weighting the
# model's level by ``n / (n + K)`` where n is how many benchmarks the model
# actually has. K=3 means a model needs ~3 real benchmarks before its own level
# outweighs the median. This stops a sparse *strong* model being flattened to
# "average" while still preventing a one-benchmark model from inflating itself.
IMPUTATION_SHRINKAGE_K = 3

# Coverage-aware weighting (see docs/sdk/routing/scoring.md). Every
# per-benchmark quality term is scaled by the share of the routable catalog that
# actually reports that benchmark, and again -- by IMPUTED_TERM_WEIGHT -- when
# the term for this particular model had to be imputed:
#
#     w(b, m) = sim(b) * BENCHMARK_WEIGHTS[b] * coverage(b) ** COVERAGE_EXPONENT
#               * (IMPUTED_TERM_WEIGHT if imputed else 1.0)
#
# Rationale: without it, a large share of the selected weight can sit on
# benchmarks only a small minority of models report, so a frontier pick may
# rest mostly on imputed values. Scaling by coverage and discounting imputed
# terms keeps the decision on measured evidence and lets more distinct models
# win. Benchmark *selection* is untouched: still the top 5 by raw similarity.
#
# COVERAGE_EXPONENT is the tuning dial -- 1.0 is linear in coverage, 0.5 is a
# half-strength version of the same rule, 0.0 disables the factor entirely.
# Both constants must match the Node SDK exactly.
IMPUTED_TERM_WEIGHT = 0.5
COVERAGE_EXPONENT = 1.0


def _compute_benchmark_medians(
    models: list[ModelInfo],
    benchmark_names: list[str],
) -> dict[str, float]:
    """Compute registry-wide raw-score medians for missing benchmark data."""
    medians: dict[str, float] = {}
    for name in benchmark_names:
        values = [
            score
            for model in models
            if (score := model.benchmark_scores.get(name)) is not None
            and math.isfinite(score)
        ]
        if not values:
            continue
        values.sort()
        medians[name] = _median(values)
    return medians


def _fixed(value: float, digits: int) -> str:
    """Half-even fixed-point formatting that never emits a signed zero.

    ``-0.0``, and any value that rounds to zero from below, would otherwise
    print as ``-0.00`` in both Python and JS; both SDKs strip the sign here so
    the reasoning string cannot disagree on it. No thousands separator: a
    four-figure $/M price must print as ``$1234.00/M`` in both SDKs.
    """
    text = f"{value:.{digits}f}"
    if text.startswith("-") and float(text) == 0.0:
        text = text[1:]
    return text


@dataclass
class _Candidate:
    """Everything known about one model before the band is applied."""

    model: ModelInfo
    quality: float
    n_real: int
    n_top: int
    imputed: int
    top_benchmarks: list[tuple[str, float]]
    cost_utility: float
    speed_utility: float
    t300: Optional[float]
    price_per_m: Optional[float]
    flags: list[str]
    no_signal: bool
    # Effective per-term quality weights (coverage- and imputation-scaled);
    # defaulted so hand-built candidates in tests stay constructible.
    benchmark_weights: dict[str, float] = field(default_factory=dict)


class ScoringEngine:
    """
    Scores models against a classified prompt (``satisficing-v1``).

    Quality opens a tolerance band below the best candidate; inside the band
    models are ranked on cost/speed alone::

        w(b,m)  = sim(b) * BENCHMARK_WEIGHTS[b] * cov(b) ** COVERAGE_EXPONENT
                  * (IMPUTED_TERM_WEIGHT if m's term for b is imputed else 1)
        q'      = sum(w * norm) / sum(w)                  # over the top-5 b
        eps     = EPS_UNIT * ((cost - 1) + (speed - 1)) / quality
        wc, ws  = (cost - 1) / 4, (speed - 1) / 4
        in_band = q' >= q'_best - eps
        sec     = (wc * U_c + ws * U_s) / (wc + ws)      # in band only
        final   = q'                if wc + ws == 0      (strict quality)
                = 0.5 + 0.5 * sec   if in band
                = 0.5 * q'          otherwise

    so every contender lands in [0.5, 1.0] and no out-of-band model can ever
    out-rank one.
    """

    def __init__(self, normalizer: Optional[BenchmarkNormalizer] = None):
        self._normalizer = normalizer or BenchmarkNormalizer()
        # Single-slot cache of the speed fallbacks, keyed by the identity of the
        # candidate list (the Router hands the same list every call). The list
        # itself is held so its id() cannot be recycled under us.
        self._stats_cache: Optional[tuple[list[ModelInfo], RegistryStats]] = None
        # Same single-slot, list-identity-keyed cache for the coverage table, so
        # a Router that hands the same candidate list every call derives it once.
        self._coverage_cache: Optional[tuple[list[ModelInfo], dict[str, float]]] = None

    def _speed_stats(self, models: list[ModelInfo]) -> RegistryStats:
        cached = self._stats_cache
        if cached is not None and cached[0] is models:
            return cached[1]
        stats = registry_speed_stats(models)
        self._stats_cache = (models, stats)
        return stats

    def _coverage(self, models: list[ModelInfo]) -> dict[str, float]:
        cached = self._coverage_cache
        if cached is not None and cached[0] is models:
            return cached[1]
        coverage = compute_benchmark_coverage(models)
        self._coverage_cache = (models, coverage)
        return coverage

    def score_models(
        self,
        models: list[ModelInfo],
        benchmark_similarities: dict[str, float],
        priorities: Priorities = DEFAULT_PRIORITIES,
        top_k: int = 5,
        registry_stats: Optional[RegistryStats] = None,
        benchmark_coverage: Optional[dict[str, float]] = None,
    ) -> list[ModelScore]:
        """
        Score and rank models based on benchmark similarities and priorities.

        Args:
            models: Available models to score.
            benchmark_similarities: Cosine similarity of user prompt to each benchmark
                                    centroid. Keys are benchmark names, values are 0-1.
            priorities: User priority weights.
            top_k: Return top K models.
            registry_stats: Speed fallbacks (median TTFT, p25 of U_s).
                Normally omitted: they are derived from ``models`` itself and
                cached per candidate-list identity.
            benchmark_coverage: ``{benchmark: n_real / N}`` over the routable
                catalog -- see :func:`compute_benchmark_coverage`. The Router
                passes ``registry.benchmark_coverage()`` so a *filtered* route
                still weights benchmarks by how well the whole catalog reports
                them. Omitted, it is derived from ``models`` itself (``:free``
                ids excluded) and cached per candidate-list identity. A
                benchmark missing from the mapping has coverage 0.0 and its
                term drops out of q' entirely.

        Returns:
            Sorted list of ModelScore objects (highest score first).
        """
        if registry_stats is None:
            registry_stats = self._speed_stats(models)
        if benchmark_coverage is None:
            benchmark_coverage = self._coverage(models)

        top_benchmarks = sorted(
            benchmark_similarities.items(), key=lambda x: x[1], reverse=True
        )[:TOP_BENCHMARKS_FOR_SCORING]
        top_benchmark_dict = dict(top_benchmarks)
        benchmark_medians = _compute_benchmark_medians(
            models,
            [name for name, _ in top_benchmarks],
        )

        candidates: list[_Candidate] = []
        for model in models:
            candidate = self._candidate(
                model,
                top_benchmark_dict,
                benchmark_medians,
                registry_stats,
                benchmark_coverage,
            )
            if candidate is not None:
                candidates.append(candidate)

        # Fallback: if NO model has a quality signal, the prompt matched no
        # benchmark at all (its embedding is orthogonal/negative to every
        # centroid, so every similarity clamped to 0). Rather than return
        # nothing -- which makes a single route() raise and a budget run report
        # the whole dataset infeasible -- re-score every model on a neutral
        # quality baseline so the prompt stays routable on cost/speed. The
        # per-model skip above still applies in the normal case where only
        # *some* models lack signal.
        if not candidates:
            for model in models:
                candidate = self._candidate(
                    model,
                    top_benchmark_dict,
                    benchmark_medians,
                    registry_stats,
                    benchmark_coverage,
                    neutral_fallback=True,
                )
                if candidate is not None:
                    candidates.append(candidate)
            if not candidates:
                return []
            return self._score_no_signal(candidates, priorities)[:top_k]

        return self._score_banded(candidates, priorities)[:top_k]

    # ----------------------------------------------------------- banded path
    def _score_banded(
        self, candidates: list[_Candidate], priorities: Priorities
    ) -> list[ModelScore]:
        eps = quality_tolerance(priorities)
        w_cost = priorities.cost_weight
        w_speed = priorities.speed_weight
        quality_best = max(c.quality for c in candidates)

        # The leader named by every reasoning string: highest q', ties broken
        # by real-evidence count then model id -- the tail of the main sort key.
        leader = min(
            candidates, key=lambda c: (-c.quality, -c.n_real, c.model.model_id)
        )

        rows: list[tuple[float, _Candidate, ModelScore]] = []
        for c in candidates:
            combined = satisficing_combine(
                q_prime=c.quality,
                u_cost_q=round(c.cost_utility, UTILITY_DECIMALS),
                u_speed_q=round(c.speed_utility, UTILITY_DECIMALS),
                q_best=quality_best,
                eps=eps,
                wc=w_cost,
                ws=w_speed,
            )
            rows.append(
                (
                    combined.utility,
                    c,
                    ModelScore(
                        model_id=c.model.model_id,
                        final_score=round(combined.final_score, SCORE_DECIMALS),
                        quality_score=round(c.quality, SCORE_DECIMALS),
                        cost_score=round(c.cost_utility, SCORE_DECIMALS),
                        speed_score=round(c.speed_utility, SCORE_DECIMALS),
                        quality_contribution=round(
                            combined.quality_contribution, SCORE_DECIMALS
                        ),
                        cost_contribution=round(
                            combined.cost_contribution, SCORE_DECIMALS
                        ),
                        speed_contribution=round(
                            combined.speed_contribution, SCORE_DECIMALS
                        ),
                        top_benchmarks=c.top_benchmarks,
                        reasoning="",
                        benchmark_weights=dict(c.benchmark_weights),
                        band_base=combined.band_base,
                        in_band=combined.in_band,
                        quality_tolerance=round(eps, SCORE_DECIMALS),
                        quality_best=round(quality_best, SCORE_DECIMALS),
                        signal_flags=list(c.flags),
                        q_prime=c.quality,
                        u_cost=c.cost_utility,
                        u_speed=c.speed_utility,
                        t300=c.t300,
                    ),
                )
            )

        # One total order, identical in both SDKs: internal utility, then
        # quality, then real (non-imputed) evidence count, then ascending model
        # id in Unicode code point order (not locale aware).
        rows.sort(
            key=lambda r: (-r[0], -r[1].quality, -r[1].n_real, r[1].model.model_id)
        )

        # The exchange-rate sentence names the same leader and the same regret
        # (that of the *selected* model) in every candidate's string, so it can
        # only be written once the winner is known.
        regret = quality_best - rows[0][1].quality
        for _, candidate, score in rows:
            score.reasoning = self._reasoning(
                candidate,
                priorities,
                eps,
                quality_best,
                score.in_band,
                leader.model.model_id,
                regret,
            )
        return [score for _, _, score in rows]

    # -------------------------------------------------------- no-signal path
    def _score_no_signal(
        self, candidates: list[_Candidate], priorities: Priorities
    ) -> list[ModelScore]:
        """All-no-signal fallback: cost/speed only, with floored weights.

        With no quality signal anywhere there is no band to compute and no
        quality to rank on -- every model sits at NEUTRAL_QUALITY_SCORE. The
        floored cost/speed weights are what make the prompt "routable on
        cost/speed" even when the user suppressed both to priority 1.
        """
        c_weight = max(priorities.cost_weight, NO_SIGNAL_WEIGHT_FLOOR)
        s_weight = max(priorities.speed_weight, NO_SIGNAL_WEIGHT_FLOOR)
        total = c_weight + s_weight

        rows: list[tuple[float, _Candidate, ModelScore]] = []
        for c in candidates:
            c_contrib = c_weight * round(c.cost_utility, UTILITY_DECIMALS) / total
            s_contrib = s_weight * round(c.speed_utility, UTILITY_DECIMALS) / total
            final = _clamp01(c_contrib + s_contrib)
            rows.append(
                (
                    final,
                    c,
                    ModelScore(
                        model_id=c.model.model_id,
                        final_score=round(final, SCORE_DECIMALS),
                        quality_score=round(c.quality, SCORE_DECIMALS),
                        cost_score=round(c.cost_utility, SCORE_DECIMALS),
                        speed_score=round(c.speed_utility, SCORE_DECIMALS),
                        quality_contribution=0.0,
                        cost_contribution=round(c_contrib, SCORE_DECIMALS),
                        speed_contribution=round(s_contrib, SCORE_DECIMALS),
                        top_benchmarks=c.top_benchmarks,
                        reasoning="No benchmark signal -- routed on cost/speed",
                        benchmark_weights=dict(c.benchmark_weights),
                        band_base=0.0,
                        in_band=False,
                        quality_tolerance=0.0,
                        quality_best=round(c.quality, SCORE_DECIMALS),
                        signal_flags=list(c.flags),
                        q_prime=c.quality,
                        u_cost=c.cost_utility,
                        u_speed=c.speed_utility,
                        t300=c.t300,
                    ),
                )
            )
        rows.sort(
            key=lambda r: (-r[0], -r[1].quality, -r[1].n_real, r[1].model.model_id)
        )
        return [score for _, _, score in rows]

    # -------------------------------------------------------- reasoning string
    def _reasoning(
        self,
        c: _Candidate,
        priorities: Priorities,
        eps: float,
        quality_best: float,
        in_band: bool,
        leader_id: str,
        regret: float,
    ) -> str:
        parts = [f"q'={_fixed(c.quality, 2)} ({c.n_real} real of {c.n_top})"]
        if c.imputed > 0:
            parts.append(f"imputed: {c.imputed}/{c.n_top}")

        if c.price_per_m is None:
            parts.append("cost unknown")
        else:
            parts.append(
                f"cost {_fixed(c.cost_utility, 4)} "
                f"(${_fixed(c.price_per_m, 2)}/M)"
            )

        if c.t300 is None:
            parts.append("speed unknown (catalog p25)")
        else:
            clause = (
                f"speed {_fixed(c.speed_utility, 4)} "
                f"({_fixed(c.t300, 2)} s to 300 tok)"
            )
            if TTFT_ESTIMATED_FLAG in c.flags:
                clause += " [ttft estimated]"
            parts.append(clause)

        if eps == 0:
            parts.append("quality only (no tolerance)")
        elif in_band:
            parts.append(
                f"within {_fixed(eps, 3)} of the best ({_fixed(quality_best, 2)})"
            )
        else:
            parts.append(
                f"{_fixed(quality_best - c.quality, 3)} below the best "
                f"({_fixed(quality_best, 2)}) -- outside the "
                f"{_fixed(eps, 3)} tolerance"
            )

        triple = f"{priorities.quality}/{priorities.cost}/{priorities.speed}"
        if eps == 0:
            parts.append(f"at {triple} only quality counts")
        else:
            parts.append(
                f"at {triple} you accept up to {_fixed(eps, 3)} less quality "
                f"for a cheaper or faster model; this pick gave up "
                f"{_fixed(regret, 2)} vs {leader_id}"
            )
        return " | ".join(parts)

    # ------------------------------------------------------------- internals
    def _model_level(self, model: ModelInfo) -> tuple[float, int]:
        """
        The model's own demonstrated quality level: the *median* of its
        normalized scores across every benchmark it has data for, plus that
        count. Used as the shrinkage target when imputing missing benchmarks so
        a strong model isn't imputed as "average". The median (rather than mean)
        keeps a single corrupt or anomalously-low score from dragging the level
        down. Returns level 0.5 (neutral) for a model with no data.
        """
        items = [
            (name, raw)
            for name, raw in model.benchmark_scores.items()
            if raw is not None and math.isfinite(raw)
        ]
        if not items:
            return 0.5, 0
        norms = sorted(self._normalizer.normalize(name, raw) for name, raw in items)
        return _median(norms), len(norms)

    def _candidate(
        self,
        model: ModelInfo,
        top_benchmarks: dict[str, float],
        benchmark_medians: dict[str, float],
        registry_stats: RegistryStats,
        benchmark_coverage: dict[str, float],
        neutral_fallback: bool = False,
    ) -> Optional[_Candidate]:
        """Quality aggregate plus cost/speed utilities for one model.

        Returns None when the model shares none of the prompt's relevant
        benchmarks, unless ``neutral_fallback`` is set (see score_models).
        """
        weighted_quality_sum = 0.0
        total_similarity_weight = 0.0
        imputed_count = 0
        model_top_benchmarks: list[tuple[str, float]] = []
        effective_weights: dict[str, float] = {}

        # The model's own demonstrated level and how much we trust it, used to
        # impute missing benchmarks via shrinkage toward the registry median.
        model_level, known_count = self._model_level(model)
        alpha = known_count / (known_count + IMPUTATION_SHRINKAGE_K)

        for benchmark_name, user_similarity in top_benchmarks.items():
            model_bench_score = model.benchmark_scores.get(benchmark_name)
            imputed = False
            # Treat a non-finite raw score (NaN/inf) as missing so it does not
            # poison the weighted quality sum; fall back to imputation instead.
            if model_bench_score is None or not math.isfinite(model_bench_score):
                median = benchmark_medians.get(benchmark_name)
                if median is None:
                    continue
                # Shrinkage imputation: blend the model's own level with the
                # registry median (in normalized space). A high-coverage strong
                # model keeps a high imputed value instead of being dragged to
                # the median; a sparse model stays near the median so it can't
                # inflate itself.
                median_norm = self._normalizer.normalize(benchmark_name, median)
                normalized = alpha * model_level + (1 - alpha) * median_norm
                imputed = True
                imputed_count += 1
            else:
                normalized = self._normalizer.normalize(
                    benchmark_name, model_bench_score
                )

            # Combine prompt-relevance (similarity) with the benchmark's
            # intrinsic importance weight, then with how much of the catalog
            # actually reports it and whether THIS model's term is evidence or
            # an imputation. similarity says "how much this prompt looks like
            # the benchmark"; weight says "how much we trust the benchmark as a
            # signal"; coverage says "how much of the catalog can be compared on
            # it at all"; IMPUTED_TERM_WEIGHT discounts a term we invented.
            coverage = benchmark_coverage.get(benchmark_name, 0.0)
            weight = (
                user_similarity
                * self._normalizer.get_weight(benchmark_name)
                * coverage**COVERAGE_EXPONENT
            )
            if imputed:
                weight *= IMPUTED_TERM_WEIGHT
            weighted_quality_sum += weight * normalized
            total_similarity_weight += weight
            effective_weights[benchmark_name] = round(weight, SCORE_DECIMALS)
            if not imputed:
                model_top_benchmarks.append((benchmark_name, normalized))

        # No usable similarity signal: this model shares none of the prompt's
        # relevant benchmarks (even after median imputation), so it is not a
        # contender. Normally drop it; when EVERY model is signal-less,
        # score_models retries with neutral_fallback=True.
        no_signal = total_similarity_weight == 0
        if no_signal and not neutral_fallback:
            return None

        quality = (
            NEUTRAL_QUALITY_SCORE
            if no_signal
            else weighted_quality_sum / total_similarity_weight
        )

        price_per_m = price_per_million(model)
        u_cost, cost_flag = cost_utility(
            None if price_per_m is None else price_per_m / 1000.0
        )
        u_speed, speed_flag, t300 = _speed_parts(model, registry_stats)

        flags: list[str] = []
        if no_signal:
            flags.append(NO_SIGNAL_FLAG)
        if cost_flag:
            flags.append(cost_flag)
        if speed_flag:
            flags.append(speed_flag)

        return _Candidate(
            model=model,
            quality=quality,
            n_real=len(model_top_benchmarks),
            n_top=len(top_benchmarks),
            imputed=imputed_count,
            top_benchmarks=model_top_benchmarks,
            benchmark_weights=effective_weights,
            cost_utility=u_cost,
            speed_utility=u_speed,
            t300=t300,
            price_per_m=price_per_m,
            flags=flags,
            no_signal=no_signal,
        )
