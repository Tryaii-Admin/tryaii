/**
 * Golden-fixture conformance for the TypeScript diagnose engine.
 *
 * The fixtures in shared/diagnose/fixtures/ were generated from the PYTHON
 * reference engine and frozen (shared/diagnose/SPEC.md §5) — this suite
 * proves the TS port reproduces every expected block byte-for-byte:
 * structural equality via toEqual PLUS JSON.stringify equality, which also
 * pins key ORDER (findings.json is byte-diffed directly across SDKs).
 *
 * All suites are routing-free: modelfit/check cases inject classification
 * via the _classification seam, so no test here needs the embedding model.
 */

import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { ModelRegistry } from '../src/registry/models.js';
import { Priorities } from '../src/scoring/priorities.js';
import {
  analyzeInventory,
  normalizeInventory,
  readDiscountFactors,
  resolveModelId,
  runCost,
  runHygiene,
  runModelFit,
} from '../src/diagnose/index.js';

const FIXTURES = join(
  dirname(fileURLToPath(import.meta.url)),
  '..', '..', '..', 'shared', 'diagnose', 'fixtures',
);

interface Case {
  name: string;
  description: string;
  input: Record<string, any>;
  expected: any;
}

function cases(suite: string): Case[] {
  const path = join(FIXTURES, suite, 'cases.json');
  if (!existsSync(path)) return [];
  return JSON.parse(readFileSync(path, 'utf-8')).cases as Case[];
}

function loadInputData(inp: Record<string, any>): unknown {
  if ('data' in inp) return inp.data;
  return JSON.parse(readFileSync(join(FIXTURES, inp.input_file), 'utf-8'));
}

/** Structural equality + serialized equality (pins key order). */
function expectFrozen(got: unknown, expected: unknown): void {
  expect(got).toEqual(expected);
  expect(JSON.stringify(got, null, 2)).toBe(JSON.stringify(expected, null, 2));
}

function registry(): ModelRegistry {
  return ModelRegistry.default();
}

function priorities(inp: Record<string, any>): Priorities {
  return Priorities.fromDict(inp.priorities ?? {});
}

const skipAll = !existsSync(FIXTURES);

describe.skipIf(skipAll)('diagnose intake fixtures', () => {
  it.each(cases('intake').map((c) => [c.name, c] as const))('%s', (_name, c) => {
    const data = loadInputData(c.input);
    if ('error' in c.expected) {
      expect(() => normalizeInventory(data)).toThrowError(c.expected.error);
      return;
    }
    expectFrozen(normalizeInventory(data), c.expected);
  });
});

describe.skipIf(skipAll)('diagnose resolve fixtures', () => {
  it.each(cases('resolve').map((c) => [c.name, c] as const))('%s', (_name, c) => {
    const [modelId, method] = resolveModelId(c.input.model, registry());
    expectFrozen({ model_id: modelId, method }, c.expected);
  });
});

describe.skipIf(skipAll)('diagnose modelfit fixtures', () => {
  it.each(cases('modelfit').map((c) => [c.name, c] as const))('%s', (_name, c) => {
    const [payload] = runModelFit(
      c.input.classification,
      c.input.resolved_model_id ?? null,
      c.input.declared_model ?? null,
      priorities(c.input),
      registry(),
    );
    expectFrozen(payload, c.expected);
  });
});

describe.skipIf(skipAll)('diagnose cost fixtures', () => {
  it.each(cases('cost').map((c) => [c.name, c] as const))('%s', (_name, c) => {
    const inp = c.input;
    let fitInternal = null;
    if ('classification' in inp) {
      [, fitInternal] = runModelFit(
        inp.classification,
        inp.resolved_model_id ?? null,
        inp.declared_model ?? null,
        priorities(inp),
        registry(),
      );
    }
    const result = runCost({
      canonicalText: inp.canonical_text ?? null,
      resolvedModelId: inp.resolved_model_id ?? null,
      declaredModel: inp.declared_model ?? null,
      registry: registry(),
      outputTokens: inp.output_tokens ?? 500,
      callsPerDay: inp.calls_per_day ?? null,
      cacheCtx: inp.cache_ctx ?? null,
      fitInternal,
      readDiscountFactors: readDiscountFactors(),
    });
    expectFrozen(result, c.expected);
  });
});

describe.skipIf(skipAll)('diagnose hygiene fixtures', () => {
  it.each(cases('hygiene').map((c) => [c.name, c] as const))('%s', (_name, c) => {
    expectFrozen(runHygiene(c.input.prompt), c.expected);
  });
});

describe.skipIf(skipAll)('diagnose check fixtures', () => {
  it.each(cases('check').map((c) => [c.name, c] as const))('%s', async (_name, c) => {
    const data = loadInputData(c.input);
    const opts = c.input.opts ?? {};
    if ('error' in c.expected) {
      await expect(analyzeInventory(data, opts)).rejects.toThrowError(c.expected.error);
      return;
    }
    expectFrozen(await analyzeInventory(data, opts), c.expected);
  });
});
