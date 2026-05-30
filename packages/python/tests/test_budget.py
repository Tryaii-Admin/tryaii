from tryaii_dre.budget import BudgetCandidate, optimize_budget_candidates


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
