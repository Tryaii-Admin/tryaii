from tryaii_dre.budget import (
    BudgetCandidate,
    compute_difficulty,
    optimize_budget_candidates,
)


def candidate(prompt_index: int, model_id: str, utility: float, cost: float) -> BudgetCandidate:
    return BudgetCandidate(
        prompt_index=prompt_index,
        model_id=model_id,
        utility=utility,
        estimated_cost=cost,
        cost_units=max(1, round(cost / 0.001)),
        input_tokens=10,
        output_tokens=20,
        final_score=utility,
        reasoning="test",
        normal_best_model=model_id,
    )


def test_optimizer_picks_best_combo_under_budget():
    result = optimize_budget_candidates(
        [
            [candidate(0, "cheap-a", 1.0, 0.001), candidate(0, "good-a", 5.0, 0.006)],
            [candidate(1, "cheap-b", 1.0, 0.001), candidate(1, "good-b", 5.0, 0.006)],
        ],
        max_price=0.007,
        cost_unit=0.001,
    )

    assert result.status == "optimal"
    assert [c.model_id for c in result.selected] in (
        ["good-a", "cheap-b"],
        ["cheap-a", "good-b"],
    )
    assert result.total_estimated_cost <= 0.007


def test_optimizer_reports_infeasible_when_cheapest_exceeds_budget():
    result = optimize_budget_candidates(
        [
            [candidate(0, "cheap-a", 1.0, 0.004)],
            [candidate(1, "cheap-b", 1.0, 0.004)],
        ],
        max_price=0.007,
        cost_unit=0.001,
    )

    assert result.status == "infeasible"
    assert result.minimum_required_budget == 0.008


def test_compute_difficulty_low_when_models_agree():
    d = compute_difficulty(
        [
            {"quality": 0.95, "cost": 0.0001},
            {"quality": 0.94, "cost": 0.001},
            {"quality": 0.96, "cost": 0.05},
        ]
    )
    assert d < 0.1


def test_compute_difficulty_high_when_only_expensive_models_win():
    d = compute_difficulty(
        [
            {"quality": 0.2, "cost": 0.0001},
            {"quality": 0.25, "cost": 0.0005},
            {"quality": 0.85, "cost": 0.05},
        ]
    )
    assert d > 0.5


def test_compute_difficulty_low_when_cheap_model_is_strong():
    d = compute_difficulty(
        [
            {"quality": 0.9, "cost": 0.0001},  # cheap AND strong
            {"quality": 0.92, "cost": 0.05},
            {"quality": 0.3, "cost": 0.0002},
        ]
    )
    assert d < 0.1


def test_compute_difficulty_zero_for_empty_or_zero_ceiling():
    assert compute_difficulty([]) == 0.0
    assert compute_difficulty([{"quality": 0.0, "cost": 1.0}]) == 0.0


def test_batch_percentile_ranks():
    from tryaii_dre.budget import _batch_percentile_ranks

    assert _batch_percentile_ranks([10, 20, 30]) == [0.0, 0.5, 1.0]
    assert _batch_percentile_ranks([30, 10, 20]) == [1.0, 0.0, 0.5]
    assert _batch_percentile_ranks([5, 5]) == [0.5, 0.5]
    assert _batch_percentile_ranks([42]) == [0.0]
    assert _batch_percentile_ranks([]) == []


def test_resolve_difficulty_selects_source():
    from tryaii_dre.budget import _resolve_difficulty

    assert _resolve_difficulty("capability", 0.8, 0.2) == 0.8
    assert _resolve_difficulty("intrinsic", 0.8, 0.2) == 0.2
    assert _resolve_difficulty("blend", 0.8, 0.2) == 0.5
    # Unknown source falls back to intrinsic (matches the Node default arm).
    assert _resolve_difficulty("???", 0.8, 0.2) == 0.2
