import { describe, expect, it } from 'vitest';
import {
  batchPercentileRanks,
  computeDifficulty,
  optimizeBudgetCandidates,
  resolveDifficulty,
  type BudgetCandidate,
} from '../src/budget.js';

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
    difficulty: 0,
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

describe('computeDifficulty', () => {
  it('is ~0 when every model scores similarly (easy / saturated)', () => {
    const d = computeDifficulty([
      { quality: 0.95, cost: 0.0001 },
      { quality: 0.94, cost: 0.001 },
      { quality: 0.96, cost: 0.05 },
    ]);
    expect(d).toBeLessThan(0.1);
  });

  it('is high when only expensive models score well (hard)', () => {
    const d = computeDifficulty([
      { quality: 0.2, cost: 0.0001 },
      { quality: 0.25, cost: 0.0005 },
      { quality: 0.85, cost: 0.05 },
    ]);
    expect(d).toBeGreaterThan(0.5);
  });

  it('stays low when a cheap model is already strong (no need to pay up)', () => {
    const d = computeDifficulty([
      { quality: 0.9, cost: 0.0001 }, // cheap AND strong
      { quality: 0.92, cost: 0.05 },
      { quality: 0.3, cost: 0.0002 },
    ]);
    expect(d).toBeLessThan(0.1);
  });

  it('returns 0 for empty input or a non-positive ceiling', () => {
    expect(computeDifficulty([])).toBe(0);
    expect(computeDifficulty([{ quality: 0, cost: 1 }])).toBe(0);
  });
});

describe('batchPercentileRanks', () => {
  it('maps values to 0..1 by rank, averaging ties', () => {
    expect(batchPercentileRanks([10, 20, 30])).toEqual([0, 0.5, 1]);
    expect(batchPercentileRanks([30, 10, 20])).toEqual([1, 0, 0.5]);
    expect(batchPercentileRanks([5, 5])).toEqual([0.5, 0.5]);
  });

  it('handles empty and single-element inputs', () => {
    expect(batchPercentileRanks([])).toEqual([]);
    expect(batchPercentileRanks([42])).toEqual([0]);
  });
});

describe('resolveDifficulty', () => {
  it("'capability' uses only the capability signal", () => {
    expect(resolveDifficulty('capability', 0.8, 0.2)).toBe(0.8);
  });

  it("'intrinsic' uses only the intrinsic signal", () => {
    expect(resolveDifficulty('intrinsic', 0.8, 0.2)).toBe(0.2);
  });

  it("'blend' averages the two", () => {
    expect(resolveDifficulty('blend', 0.8, 0.2)).toBeCloseTo(0.5, 10);
  });
});
