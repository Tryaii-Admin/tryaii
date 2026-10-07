/**
 * Cross-source benchmark-name consistency guard for every catalog bundle.
 *
 * Benchmark NAMES are the join key between the five files of a catalog bundle:
 * the taxonomy (benchmarks.json), the normalization ranges, the classifier
 * centroids, the training queries, and the model scores (models.json). When
 * they drift, prompts can match benchmarks no model carries data for and score
 * zero models. These tests run on the packaged starter bundle and, when built,
 * the full bundle, and fail loudly the moment any source drifts.
 *
 * Mirrors packages/python/tests/test_benchmarks/test_consistency.py.
 */

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, it, expect } from 'vitest';

import { BenchmarkRegistry } from '../../src/benchmarks/registry.js';
import { STANDARD_BENCHMARKS } from '../../src/benchmarks/standard.js';
import type { CatalogBundle } from '../../src/catalog/bundle.js';
import { benchmarkFingerprint } from '../../src/centroids/loader.js';
import { BENCHMARK_CATEGORIES } from '../../src/classifiers/embedding.js';
import {
  BENCHMARK_WEIGHTS,
  NORMALIZATION_RANGES,
  RANDOM_CHANCE_FLOORS,
} from '../../src/scoring/benchmarks.js';
import { FULL_ONLY, HAS_FULL_BUNDLE, fullBundle, starterBundle } from '../_catalog.js';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'src');

function bundleSuite(label: string, get: () => CatalogBundle): void {
  const registryNames = (b: CatalogBundle) => {
    const names = new Set<string>();
    for (const model of b.modelEntries()) {
      for (const name of Object.keys(model.benchmark_scores ?? {})) names.add(name);
    }
    return names;
  };
  const centroidNames = (b: CatalogBundle) => new Set(Object.keys(b.centroids.centroids));
  const trainingNames = (b: CatalogBundle) => new Set(Object.keys(b.trainingQueries.benchmarks));
  const rangeNames = (b: CatalogBundle) => new Set(Object.keys(b.rangeEntries()));

  it(`${label}: centroid keys match training query keys`, () => {
    expect(centroidNames(get())).toEqual(trainingNames(get()));
  });

  it(`${label}: taxonomy names are unique and match the range keys`, () => {
    const names = get().benchmarkNames;
    expect(names.length).toBe(new Set(names).size);
    expect(new Set(names)).toEqual(rangeNames(get()));
  });

  it(`${label}: every registry benchmark has a normalization range`, () => {
    const ranges = rangeNames(get());
    expect([...registryNames(get())].filter((n) => !ranges.has(n))).toEqual([]);
  });

  it(`${label}: every centroid benchmark has a range and a category`, () => {
    const ranges = rangeNames(get());
    const categories = get().benchmarkCategories();
    expect([...centroidNames(get())].filter((n) => !ranges.has(n))).toEqual([]);
    expect([...centroidNames(get())].filter((n) => !(n in categories))).toEqual([]);
  });

  it(`${label}: every weight is positive and finite; every family is declared`, () => {
    const bad = Object.entries(get().benchmarkWeights()).filter(
      ([, w]) => !Number.isFinite(w) || w <= 0,
    );
    expect(bad).toEqual([]);
    const families = new Set(get().benchmarks.families.map((f) => f.id));
    expect(get().benchmarkEntries.filter((e) => !families.has(e.family))).toEqual([]);
  });

  it(`${label}: random-chance floors reference known benchmarks only`, () => {
    const ranges = rangeNames(get());
    expect(Object.keys(get().randomChanceFloors()).filter((n) => !ranges.has(n))).toEqual([]);
  });

  it(`${label}: centroid fingerprint matches the training-query benchmark set`, () => {
    const meta = get().centroids.metadata;
    expect(meta.benchmark_fingerprint).toBe(benchmarkFingerprint([...trainingNames(get())]));
    expect(meta.benchmark_count).toBe(trainingNames(get()).size);
  });

  it(`${label}: BenchmarkRegistry.fromBundle carries the taxonomy`, () => {
    const registry = BenchmarkRegistry.fromBundle(get());
    expect(registry.names).toEqual(get().benchmarkNames);
    const normalizer = registry.getNormalizer();
    for (const [name, weight] of Object.entries(get().benchmarkWeights())) {
      expect(normalizer.getWeight(name)).toBe(weight);
    }
    expect(registry.randomChanceFloors()).toEqual(get().randomChanceFloors());
  });
}

describe('catalog bundle consistency (starter)', () => {
  bundleSuite('starter', starterBundle);
});

describe.skipIf(!HAS_FULL_BUNDLE)(`catalog bundle consistency (full) ${FULL_ONLY}`, () => {
  bundleSuite('full', fullBundle);
});

describe('starter module tables are the starter bundle', () => {
  it('benchmark weight keys match normalization keys', () => {
    expect(new Set(Object.keys(BENCHMARK_WEIGHTS))).toEqual(
      new Set(Object.keys(NORMALIZATION_RANGES)),
    );
  });

  it('standard benchmark names are unique and are the starter bundle, in order', () => {
    const names = STANDARD_BENCHMARKS.map((b) => b.name);
    expect(names.length).toBe(new Set(names).size);
    expect(new Set(names)).toEqual(new Set(Object.keys(NORMALIZATION_RANGES)));
    expect(names).toEqual(starterBundle().benchmarkNames);
  });

  it('every standard benchmark has a category', () => {
    const missing = STANDARD_BENCHMARKS.filter((b) => !(b.name in BENCHMARK_CATEGORIES));
    expect(missing).toEqual([]);
  });

  it('random-chance floors are the starter floors', () => {
    expect(RANDOM_CHANCE_FLOORS).toEqual(starterBundle().randomChanceFloors());
  });

  it('standard benchmark ranges come from the normalization table', () => {
    for (const b of STANDARD_BENCHMARKS) {
      const r = NORMALIZATION_RANGES[b.name];
      expect([b.normalization.minScore, b.normalization.maxScore]).toEqual([
        r.minScore,
        r.maxScore,
      ]);
    }
  });

  it('normalization ranges match the packaged starter JSON exactly', () => {
    // The starter bundle is synced byte-identically into both SDKs
    // (test_parity.py compares the copies). This is the Node half: the
    // in-memory table must be exactly what the shipped JSON says, so a stale
    // load cannot silently rescale every model.
    const raw = JSON.parse(
      readFileSync(join(SRC, 'catalog', 'data', 'starter', 'normalization_ranges.json'), 'utf-8'),
    ) as {
      benchmarks: Record<string, { lo: number; hi: number; description: string }>;
    };
    expect(new Set(Object.keys(NORMALIZATION_RANGES))).toEqual(
      new Set(Object.keys(raw.benchmarks)),
    );
    for (const [name, entry] of Object.entries(raw.benchmarks)) {
      const r = NORMALIZATION_RANGES[name];
      expect([r.minScore, r.maxScore, r.description]).toEqual([
        entry.lo,
        entry.hi,
        entry.description,
      ]);
    }
  });
});
