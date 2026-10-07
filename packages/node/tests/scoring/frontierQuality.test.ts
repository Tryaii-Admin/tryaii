/**
 * Generic frontier-quality guards (Node side).
 *
 * When the user asks for quality above all, the genuinely best models must
 * win -- including models that do not exist yet. No real model names appear in
 * any assertion, so these guards cannot be satisfied by overfitting to today's
 * leaderboard. Mirrors packages/python/tests/test_scoring/test_frontier_quality.py.
 */

import { describe, it, expect } from 'vitest';

import { ModelInfo, ModelPricing, ModelRegistry } from '../../src/registry/models.js';
import { BenchmarkNormalizer, NORMALIZATION_RANGES } from '../../src/scoring/benchmarks.js';
import { ScoringEngine } from '../../src/scoring/engine.js';
import { Priorities } from '../../src/scoring/priorities.js';

const QUALITY_ONLY = new Priorities(5, 1, 1);

const TASK_SIGNALS: Record<string, Record<string, number>> = {
  coding: { 'Chatbot Arena Elo (Code)': 0.62, 'LiveCodeBench': 0.6, 'SWE-bench-verified': 0.58, 'HumanEval': 0.53 },
  reasoning: { 'GPQA': 0.62, 'HLE': 0.6, 'MMLU-Pro': 0.58, 'MMLU': 0.53 },
  chat: { 'Chatbot Arena Elo': 0.62, 'IFBench': 0.58, 'IFEval': 0.53 },
  math: { 'AIME-2025': 0.62, 'MATH': 0.6, 'GSM8K': 0.55 },
};

function championAll(): ModelInfo {
  const scores: Record<string, number> = {};
  for (const [b, r] of Object.entries(NORMALIZATION_RANGES)) scores[b] = r.maxScore;
  return new ModelInfo({
    modelId: 'future/champion',
    provider: 'future',
    benchmarkScores: scores,
    pricing: new ModelPricing(0.002, 0.008),
    latency: 'fast',
  });
}

describe('future models win quality-first routing', () => {
  it('a dominant new model ranks first on every task signal', () => {
    const registry = ModelRegistry.default();
    const engine = new ScoringEngine();
    const models = [...registry.allModels, championAll()];
    for (const [task, sims] of Object.entries(TASK_SIGNALS)) {
      const top = engine.scoreModels(models, sims, QUALITY_ONLY, 1)[0];
      expect(top.modelId, `dominant model lost the ${task} signal`).toBe('future/champion');
    }
  });

  it('a sparse-but-stellar new model wins its covered signals', () => {
    const covered = [
      'Chatbot Arena Elo', 'Chatbot Arena Elo (Code)', 'GPQA', 'HLE',
      'LiveCodeBench', 'MMLU-Pro', 'Tau2-bench', 'IFBench',
    ];
    const scores: Record<string, number> = {};
    for (const b of covered) scores[b] = NORMALIZATION_RANGES[b].maxScore;
    const sparseChampion = new ModelInfo({
      modelId: 'future/sparse-champion',
      provider: 'future',
      benchmarkScores: scores,
      pricing: new ModelPricing(0.002, 0.008),
      latency: 'fast',
    });
    const registry = ModelRegistry.default();
    const engine = new ScoringEngine();
    const sims = { 'Chatbot Arena Elo (Code)': 0.62, 'LiveCodeBench': 0.6, 'GPQA': 0.55 };
    const top = engine.scoreModels([...registry.allModels, sparseChampion], sims, QUALITY_ONLY, 1)[0];
    expect(top.modelId).toBe('future/sparse-champion');
  });

  it('quality-only ignores the dominant model being expensive and slow', () => {
    const base = championAll();
    const expensive = new ModelInfo({
      modelId: base.modelId,
      provider: base.provider,
      benchmarkScores: base.benchmarkScores,
      pricing: new ModelPricing(0.05, 0.25),
      latency: 'very slow',
    });
    const registry = ModelRegistry.default();
    const engine = new ScoringEngine();
    const top = engine.scoreModels(
      [...registry.allModels, expensive],
      TASK_SIGNALS['reasoning'],
      QUALITY_ONLY,
      1,
    )[0];
    expect(top.modelId).toBe('future/champion');
  });
});

describe('catalog quality calibration', () => {
  function levels(): Map<string, number> {
    const registry = ModelRegistry.default();
    const n = new BenchmarkNormalizer();
    const out = new Map<string, number>();
    for (const m of registry.allModels) {
      const vals = Object.entries(m.benchmarkScores)
        .map(([b, v]) => n.normalize(b, v))
        .sort((a, b) => a - b);
      const mid = Math.floor(vals.length / 2);
      out.set(m.modelId, vals.length % 2 === 1 ? vals[mid] : (vals[mid - 1] + vals[mid]) / 2);
    }
    return out;
  }

  it('Arena top-10 models have healthy benchmark levels', () => {
    const registry = ModelRegistry.default();
    const lv = levels();
    const byArena = registry.allModels
      .filter((m) => m.benchmarkScores['Chatbot Arena Elo'] !== undefined)
      .sort((a, b) => a.benchmarkScores['Chatbot Arena Elo'] - b.benchmarkScores['Chatbot Arena Elo']);
    const top10 = byArena.slice(-10).map((m) => lv.get(m.modelId)!).sort((a, b) => a - b);
    const top10Median = top10[Math.floor(top10.length / 2)];

    const all = [...lv.values()].sort((a, b) => a - b);
    const p70 = all[Math.floor(0.7 * all.length)];
    expect(top10Median).toBeGreaterThanOrEqual(p70);
  });

  it('no top-decile Arena model has a near-zero overall level', () => {
    // Under catalog-derived ranges a single near-zero *term* is expected and
    // healthy: the floor is the benchmark's p25, so a quarter of the catalog
    // normalises to 0 on any given benchmark, frontier models included (they
    // are frontier on the benchmarks they publish, not on all 33). What must
    // never happen is a top-decile model whose whole demonstrated level
    // collapses -- that is what used to make the quality term meaningless.
    const registry = ModelRegistry.default();
    const lv = levels();
    const byArena = registry.allModels
      .filter((m) => m.benchmarkScores['Chatbot Arena Elo'] !== undefined)
      .sort((a, b) => a.benchmarkScores['Chatbot Arena Elo'] - b.benchmarkScores['Chatbot Arena Elo']);
    const decile = Math.max(1, Math.floor(byArena.length / 10));
    const suspicious = byArena
      .slice(-decile)
      .filter((m) => lv.get(m.modelId)! < 0.2)
      .map((m) => `${m.modelId}:level=${lv.get(m.modelId)!.toFixed(3)}`);
    expect(suspicious).toEqual([]);
  });
});
