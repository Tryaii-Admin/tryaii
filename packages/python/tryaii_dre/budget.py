"""Budget-aware dataset routing utilities.

This module solves the routing budget problem as a multiple-choice knapsack:
for each prompt, choose one model candidate; maximize total utility while
keeping the total estimated generation cost under a shared budget.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable, Optional

from tryaii_dre.router import Router, RouteResult
from tryaii_dre.scoring.priorities import Priorities


@dataclass(frozen=True)
class BudgetCandidate:
    prompt_index: int
    model_id: str
    utility: float
    estimated_cost: float
    cost_units: int
    input_tokens: int
    output_tokens: int
    final_score: float
    reasoning: str
    normal_best_model: str
    # Per-prompt difficulty in [0, 1] (capability sensitivity). Same value for
    # every candidate of a prompt; surfaced so eval/consumers can report it.
    difficulty: float = 0.0


@dataclass
class BudgetOptimizationResult:
    status: str
    selected: list[BudgetCandidate]
    total_estimated_cost: float
    minimum_required_budget: float
    budget: float
    cost_unit: float
    message: str = ""
    budget_mode: str = "strict"
    requested_output_tokens: Optional[int] = None
    effective_output_tokens: Optional[int] = None
    requested_minimum_required_budget: Optional[float] = None
    budget_shortfall: float = 0.0


@dataclass
class BudgetedRouteResult:
    route_result: RouteResult
    selected: BudgetCandidate
    cumulative_cost: float
    remaining_budget: float
    route_ms: float


def estimate_tokens(text: str) -> int:
    """Approximate token count with a deterministic 4 chars ~= 1 token rule."""
    return max(1, math.ceil(len(text) / 4))


# Default difficulty amplification for the budget knapsack.
#
# utility = quality * (1 + DEFAULT_DIFFICULTY_GAMMA * difficulty), so a maximally
# hard prompt (difficulty = 1) counts up to (1 + gamma)x as much as an easy one in
# the shared-budget optimization. This is the lever that makes the optimizer invest
# more in complex prompts. Must stay in sync with the Node SDK's
# DEFAULT_DIFFICULTY_GAMMA (budget.ts).
DEFAULT_DIFFICULTY_GAMMA = 1.0

# Which difficulty signal drives allocation: "intrinsic" (content-based easy/hard
# centroid distance, default), "capability" (model quality vs price spread), or
# "blend" (mean of the two). Must stay in sync with the Node SDK.
DEFAULT_DIFFICULTY_SOURCE = "intrinsic"


def _resolve_difficulty(source: str, capability: float, intrinsic: float) -> float:
    """Combine the two difficulty signals per the chosen source: 'capability'
    (model-spread only), 'intrinsic' (content-based only, default), or 'blend'
    (mean). Must stay in sync with the Node SDK's resolveDifficulty (budget.ts)."""
    if source == "capability":
        return capability
    if source == "blend":
        return 0.5 * (capability + intrinsic)
    return intrinsic


def compute_difficulty(points: list[dict]) -> float:
    """Per-prompt difficulty = capability sensitivity: how much achievable quality
    depends on which model you pick.

        difficulty = (q_top - q_cheap) / q_top      (clamped to [0, 1])

      - difficulty ~ 0 -> a cheap model is about as good as the frontier -> EASY
      - difficulty ~ 1 -> only expensive models reach the top quality     -> HARD

    ``q_cheap`` is the best quality among the cheapest third of candidates (>= 1).
    Using best-cheap vs best-overall (not min vs max) stops one junk model from
    inflating difficulty and correctly reports EASY when a cheap-but-strong model
    exists. Returns 0 for empty input or a non-positive ceiling. Must stay in sync
    with the Node SDK's computeDifficulty (budget.ts).
    """
    if not points:
        return 0.0
    q_top = max(p["quality"] for p in points)
    if not (q_top > 0):
        return 0.0
    by_cost = sorted(points, key=lambda p: p["cost"])
    tier_size = max(1, len(by_cost) // 3)
    q_cheap = max(p["quality"] for p in by_cost[:tier_size])
    difficulty = (q_top - q_cheap) / q_top
    return max(0.0, min(1.0, difficulty))


def _batch_percentile_ranks(values: list[float]) -> list[float]:
    """Map values to their rank within the batch, scaled to [0, 1] (smallest -> 0,
    largest -> 1, ties share the average rank). Spreads a compressed difficulty
    signal across the full range so relative ordering -- not tiny absolute
    differences -- drives allocation. Empty/single inputs map to 0. Must stay in
    sync with the Node SDK's batchPercentileRanks (budget.ts)."""
    n = len(values)
    if n <= 1:
        return [0.0 for _ in values]
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg / (n - 1)
        i = j + 1
    return ranks


def estimate_generation_cost(model, input_tokens: int, output_tokens: int) -> Optional[float]:
    """Estimate USD generation cost for a model, or None when pricing is missing."""
    if not model or not model.pricing:
        return None
    if not (
        math.isfinite(model.pricing.input_per_1k)
        and math.isfinite(model.pricing.output_per_1k)
    ):
        # Non-finite pricing must surface as None, never a NaN cost that would
        # poison feasibility math and sorts downstream.
        return None
    cost = (
        (input_tokens / 1000) * model.pricing.input_per_1k
        + (output_tokens / 1000) * model.pricing.output_per_1k
    )
    return cost if math.isfinite(cost) else None


def _cost_unit_for_budget(max_price: float) -> float:
    # Scale the unit to the budget so budget_units stays ~constant (~10k) for
    # any budget size: fine *relative* resolution without an absolute floor (the
    # old 1e-5 floor collapsed resolution for sub-cent budgets) and a bounded DP
    # state space -- a fixed max_price/1e6 made budget_units ~1e6 for a $4 budget
    # and exhausted memory. Correctness comes from the float feasibility gate +
    # cheapest fallback, so this only affects optimization granularity.
    if max_price <= 0:
        return 1e-9
    return max_price / 10_000


def _candidate_sort_key(candidate: BudgetCandidate) -> tuple[float, float, str]:
    return (candidate.estimated_cost, -candidate.utility, candidate.model_id)


def pareto_prune(candidates: list[BudgetCandidate]) -> list[BudgetCandidate]:
    """Drop candidates that are both more expensive and no better in utility."""
    ordered = sorted(candidates, key=_candidate_sort_key)
    kept: list[BudgetCandidate] = []
    best_utility = -math.inf
    for candidate in ordered:
        if candidate.utility > best_utility + 1e-12:
            kept.append(candidate)
            best_utility = candidate.utility
    return kept


def optimize_budget_candidates(
    candidate_groups: list[list[BudgetCandidate]],
    max_price: float,
    cost_unit: Optional[float] = None,
) -> BudgetOptimizationResult:
    """Choose one candidate per prompt under a shared budget."""
    if max_price < 0:
        raise ValueError("max_price must be non-negative")
    if not candidate_groups:
        return BudgetOptimizationResult("optimal", [], 0.0, 0.0, max_price, cost_unit or 0.00001)

    unit = cost_unit or _cost_unit_for_budget(max_price)
    budget_units = math.floor(max_price / unit)
    groups = [pareto_prune(group) for group in candidate_groups]

    if any(not group for group in groups):
        return BudgetOptimizationResult(
            "infeasible",
            [],
            0.0,
            math.inf,
            max_price,
            unit,
            "At least one prompt has no priced model candidates.",
        )

    cheapest = [min(group, key=_candidate_sort_key) for group in groups]
    minimum_required_budget = sum(candidate.estimated_cost for candidate in cheapest)
    if minimum_required_budget > max_price:
        cumulative = 0.0
        selected = []
        for candidate in cheapest:
            cumulative += candidate.estimated_cost
            selected.append(candidate)
        return BudgetOptimizationResult(
            "infeasible",
            selected,
            cumulative,
            minimum_required_budget,
            max_price,
            unit,
            "Budget is below the minimum cost required to route every prompt.",
        )

    # states[cost_units] = (utility, previous_cost_units, candidate_index)
    states: dict[int, tuple[float, Optional[int], Optional[int]]] = {
        0: (0.0, None, None)
    }
    layers: list[dict[int, tuple[float, Optional[int], Optional[int]]]] = []

    for group in groups:
        next_states: dict[int, tuple[float, Optional[int], Optional[int]]] = {}
        for previous_cost, (previous_utility, _, _) in states.items():
            for idx, candidate in enumerate(group):
                new_cost = previous_cost + candidate.cost_units
                if new_cost > budget_units:
                    continue
                new_utility = previous_utility + candidate.utility
                existing = next_states.get(new_cost)
                if existing is None or new_utility > existing[0] + 1e-12:
                    next_states[new_cost] = (new_utility, previous_cost, idx)

        if not next_states:
            # Float feasibility was already proven by the minimum-cost gate
            # above, so the integer DP failing to seat a combination is a
            # quantization artefact, not true infeasibility. Fall back to the
            # cheapest-per-prompt assignment as an optimal selection.
            return BudgetOptimizationResult(
                "optimal",
                cheapest,
                minimum_required_budget,
                minimum_required_budget,
                max_price,
                unit,
            )

        pruned: dict[int, tuple[float, Optional[int], Optional[int]]] = {}
        best_seen = -math.inf
        for cost in sorted(next_states):
            state = next_states[cost]
            if state[0] > best_seen + 1e-12:
                pruned[cost] = state
                best_seen = state[0]
        states = pruned
        layers.append(states)

    if not states:
        # No surviving DP state but float feasibility holds: return cheapest.
        return BudgetOptimizationResult(
            "optimal",
            cheapest,
            minimum_required_budget,
            minimum_required_budget,
            max_price,
            unit,
        )

    best_cost, _ = max(
        states.items(),
        key=lambda item: (item[1][0], -item[0]),
    )

    selected: list[BudgetCandidate] = [cheapest[0]] * len(groups)
    current_cost = best_cost
    for prompt_index in range(len(groups) - 1, -1, -1):
        _, previous_cost, candidate_index = layers[prompt_index][current_cost]
        if previous_cost is None or candidate_index is None:
            raise RuntimeError("invalid optimizer backpointer")
        selected[prompt_index] = groups[prompt_index][candidate_index]
        current_cost = previous_cost

    total_cost = sum(candidate.estimated_cost for candidate in selected)
    return BudgetOptimizationResult(
        "optimal",
        selected,
        total_cost,
        minimum_required_budget,
        max_price,
        unit,
    )


def _reprice_candidate(
    router: Router,
    candidate: BudgetCandidate,
    output_tokens: int,
    cost_unit: float,
) -> Optional[BudgetCandidate]:
    model = router.models.get_model(candidate.model_id)
    estimated_cost = estimate_generation_cost(model, candidate.input_tokens, output_tokens)
    if estimated_cost is None or not math.isfinite(estimated_cost):
        return None
    return BudgetCandidate(
        **{
            **candidate.__dict__,
            "estimated_cost": estimated_cost,
            # Zero-cost candidates consume zero budget units; only clamp negatives.
            "cost_units": max(0, math.ceil(estimated_cost / cost_unit)),
            "output_tokens": output_tokens,
        }
    )


def _reprice_candidate_groups(
    router: Router,
    candidate_groups: list[list[BudgetCandidate]],
    output_tokens: int,
    cost_unit: float,
) -> list[list[BudgetCandidate]]:
    repriced: list[list[BudgetCandidate]] = []
    for group in candidate_groups:
        repriced_group: list[BudgetCandidate] = []
        for candidate in group:
            repriced_candidate = _reprice_candidate(router, candidate, output_tokens, cost_unit)
            if repriced_candidate is not None:
                repriced_group.append(repriced_candidate)
        repriced.append(repriced_group)
    return repriced


def _minimum_required_units(candidate_groups: list[list[BudgetCandidate]]) -> int:
    if any(not group for group in candidate_groups):
        return math.inf
    return sum(min(candidate.cost_units for candidate in group) for group in candidate_groups)


def _fit_output_tokens(
    router: Router,
    candidate_groups: list[list[BudgetCandidate]],
    requested_output_tokens: int,
    max_price: float,
    cost_unit: float,
) -> tuple[int, list[list[BudgetCandidate]]]:
    """Find the largest positive output length whose cheapest assignment fits."""
    budget_units = math.floor(max_price / cost_unit)
    best_tokens = -1
    best_groups: list[list[BudgetCandidate]] = []
    # Require a positive output-token floor: an "optimal" result with zero
    # output tokens is meaningless, so the search starts at 1.
    low = 1
    high = requested_output_tokens

    while low <= high:
        mid = (low + high) // 2
        repriced = _reprice_candidate_groups(router, candidate_groups, mid, cost_unit)
        if _minimum_required_units(repriced) <= budget_units:
            best_tokens = mid
            best_groups = repriced
            low = mid + 1
        else:
            high = mid - 1

    return best_tokens, best_groups


def build_budget_candidates(
    router: Router,
    prompt: str,
    prompt_index: int,
    priorities: Priorities,
    output_tokens: int,
    difficulty_source: str = DEFAULT_DIFFICULTY_SOURCE,
) -> tuple[RouteResult, list[BudgetCandidate]]:
    """Route one prompt and convert every scored model to a budget candidate."""
    all_model_count = len(router.models.all_models)
    route_result = router.route(prompt, priorities=priorities, top_k=all_model_count)
    input_tokens = estimate_tokens(prompt)

    # Price every model first so difficulty can be read off the (cost, quality)
    # spread before utilities are assigned.
    priced = []
    for score in route_result.scores:
        model = router.models.get_model(score.model_id)
        estimated_cost = estimate_generation_cost(model, input_tokens, output_tokens)
        if estimated_cost is None or not math.isfinite(estimated_cost):
            continue
        priced.append((score, estimated_cost))

    # Difficulty = capability sensitivity (see compute_difficulty). The factor
    # scales every candidate of THIS prompt equally, so it only raises the
    # prompt's weight in the cross-prompt knapsack -- which is how harder prompts
    # win more budget. The old `confidence` multiplier is dropped on purpose: it
    # tracked category-match clarity (often higher for easy prompts), not
    # difficulty.
    # Pick the difficulty signal. capability = how much model choice changes
    # quality (compute_difficulty); intrinsic = content-based easy/hard centroid
    # distance from the classifier; blend = mean of the two. Intrinsic falls back
    # to capability when the classifier didn't supply it (e.g. an empty centroid set).
    capability_difficulty = compute_difficulty(
        [{"quality": score.quality_score, "cost": cost} for score, cost in priced]
    )
    intrinsic_difficulty = (
        route_result.classification.difficulty
        if route_result.classification is not None
        else capability_difficulty
    )
    difficulty = _resolve_difficulty(
        difficulty_source, capability_difficulty, intrinsic_difficulty
    )
    # gamma is applied later in route_dataset_with_budget, AFTER batch-normalizing
    # difficulty across all prompts; here utility is just raw quality.
    candidate_inputs = [
        (score, cost, score.quality_score) for score, cost in priced
    ]

    # Tie-break: highest utility, then lowest cost, then smallest modelId
    # (Unicode code point order). Using min over negated utility keeps the
    # smallest-id winner deterministic.
    quality_best_model = (
        min(candidate_inputs, key=lambda item: (-item[2], item[1], item[0].model_id))[0].model_id
        if candidate_inputs
        else route_result.best_model
    )

    candidates: list[BudgetCandidate] = []
    for score, estimated_cost, utility in candidate_inputs:
        candidates.append(
            BudgetCandidate(
                prompt_index=prompt_index,
                model_id=score.model_id,
                utility=utility,
                estimated_cost=estimated_cost,
                cost_units=0,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                final_score=score.final_score,
                reasoning=score.reasoning,
                normal_best_model=quality_best_model,
                difficulty=difficulty,
            )
        )
    return route_result, candidates


def route_dataset_with_budget(
    router: Router,
    prompts: list[str],
    priorities: Priorities,
    max_price: float,
    output_tokens: int,
    budget_mode: str = "strict",
    difficulty_gamma: float = DEFAULT_DIFFICULTY_GAMMA,
    difficulty_source: str = DEFAULT_DIFFICULTY_SOURCE,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> tuple[list[BudgetedRouteResult], BudgetOptimizationResult]:
    """Route a dataset with a shared generation budget."""
    if output_tokens < 0:
        raise ValueError("output_tokens must be non-negative")
    if budget_mode not in {"strict", "fit-output"}:
        raise ValueError("budget_mode must be 'strict' or 'fit-output'")

    cost_unit = _cost_unit_for_budget(max_price)
    route_results: list[RouteResult] = []
    route_times: list[float] = []
    candidate_groups: list[list[BudgetCandidate]] = []
    quality_priorities = Priorities.performance()
    for idx, prompt in enumerate(prompts):
        started = time.perf_counter()
        route_result, candidates = build_budget_candidates(
            router,
            prompt,
            idx,
            quality_priorities,
            output_tokens,
            difficulty_source,
        )
        route_times.append(round((time.perf_counter() - started) * 1000, 2))
        route_results.append(route_result)
        candidate_groups.append(
            [
                BudgetCandidate(
                    **{
                        **candidate.__dict__,
                        # Zero-cost candidates consume zero budget units.
                        "cost_units": max(0, math.ceil(candidate.estimated_cost / cost_unit)),
                    }
                )
                for candidate in candidates
            ]
        )
        if progress_callback:
            progress_callback(idx + 1, len(prompts))

    # Fix 1: batch-normalize difficulty to each prompt's rank within the batch
    # (0 = easiest, 1 = hardest), then apply utility *= 1 + gamma * rank. Raw
    # capability-sensitivity is compressed (cheap models are strong), so ranking
    # stretches the signal to a full range and lets the knapsack reallocate toward
    # the relatively-harder prompts. Must stay in sync with the Node SDK.
    raw_difficulties = [group[0].difficulty if group else 0.0 for group in candidate_groups]
    difficulty_ranks = _batch_percentile_ranks(raw_difficulties)
    candidate_groups = [
        [
            BudgetCandidate(
                **{
                    **c.__dict__,
                    "utility": c.utility * (1.0 + difficulty_gamma * difficulty_ranks[i]),
                }
            )
            for c in group
        ]
        for i, group in enumerate(candidate_groups)
    ]

    requested_output_tokens = output_tokens
    optimization = optimize_budget_candidates(candidate_groups, max_price, cost_unit)
    optimization.budget_mode = budget_mode
    optimization.requested_output_tokens = requested_output_tokens
    optimization.effective_output_tokens = output_tokens
    optimization.requested_minimum_required_budget = optimization.minimum_required_budget
    optimization.budget_shortfall = max(0.0, optimization.minimum_required_budget - max_price)

    if (
        budget_mode == "fit-output"
        and optimization.status == "infeasible"
        and output_tokens > 0
        and optimization.minimum_required_budget != math.inf
    ):
        fitted_tokens, fitted_groups = _fit_output_tokens(
            router,
            candidate_groups,
            requested_output_tokens,
            max_price,
            cost_unit,
        )
        # Only accept a positive output-token fit; 0 means nothing fit and the
        # workload stays infeasible for fit-output mode.
        if fitted_tokens > 0:
            fitted_optimization = optimize_budget_candidates(fitted_groups, max_price, cost_unit)
            fitted_optimization.budget_mode = budget_mode
            fitted_optimization.requested_output_tokens = requested_output_tokens
            fitted_optimization.effective_output_tokens = fitted_tokens
            fitted_optimization.requested_minimum_required_budget = (
                optimization.minimum_required_budget
            )
            fitted_optimization.budget_shortfall = max(
                0.0,
                optimization.minimum_required_budget - max_price,
            )
            if fitted_optimization.status == "optimal":
                fitted_optimization.message = (
                    "Requested output tokens did not fit the budget; "
                    f"optimized with {fitted_tokens} output tokens per prompt."
                )
                optimization = fitted_optimization

    selected_by_index = {candidate.prompt_index: candidate for candidate in optimization.selected}

    results: list[BudgetedRouteResult] = []
    cumulative = 0.0
    for idx, route_result in enumerate(route_results):
        selected = selected_by_index.get(idx)
        if selected is None:
            continue
        cumulative += selected.estimated_cost
        results.append(
            BudgetedRouteResult(
                route_result=route_result,
                selected=selected,
                cumulative_cost=cumulative,
                remaining_budget=max_price - cumulative,
                route_ms=route_times[idx],
            )
        )

    return results, optimization
