import { describe, expect, it } from 'vitest';
import { optimizeBudgetCandidates, type BudgetCandidate } from '../src/budget.js';

function candidate(
  promptIndex: number,
  modelId: string,
  utility: number,
  estimatedCost: number,
): BudgetCandidate {
  return {
    promptIndex,
    modelId,
    utility,
    estimatedCost,
    costUnits: Math.max(1, Math.round(estimatedCost / 0.001)),
    inputTokens: 10,
    outputTokens: 20,
    finalScore: utility,
    reasoning: 'test',
    normalBestModel: modelId,
  };
}

describe('optimizeBudgetCandidates', () => {
  it('picks the best candidate combination under budget', () => {
    const result = optimizeBudgetCandidates(
      [
        [candidate(0, 'cheap-a', 1, 0.001), candidate(0, 'good-a', 5, 0.006)],
        [candidate(1, 'cheap-b', 1, 0.001), candidate(1, 'good-b', 5, 0.006)],
      ],
      0.007,
      0.001,
    );

    expect(result.status).toBe('optimal');
    expect([
      ['good-a', 'cheap-b'],
      ['cheap-a', 'good-b'],
    ]).toContainEqual(result.selected.map((c) => c.modelId));
    expect(result.totalEstimatedCost).toBeLessThanOrEqual(0.007);
  });

  it('reports infeasible when the cheapest full assignment exceeds budget', () => {
    const result = optimizeBudgetCandidates(
      [
        [candidate(0, 'cheap-a', 1, 0.004)],
        [candidate(1, 'cheap-b', 1, 0.004)],
      ],
      0.007,
      0.001,
    );

    expect(result.status).toBe('infeasible');
    expect(result.minimumRequiredBudget).toBe(0.008);
  });
});
