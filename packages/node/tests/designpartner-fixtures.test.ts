/**
 * Golden-fixture conformance for the TypeScript designpartner engine.
 *
 * The fixtures in shared/designpartner/fixtures/ were generated from the
 * PYTHON reference engine and frozen (SPEC.md §7) — this suite proves the
 * TS port reproduces every expected block byte-for-byte: structural
 * equality via toEqual PLUS JSON.stringify equality, which also pins key
 * ORDER. The cli suite is exercised by test_designpartner_cli_parity.py.
 */

import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import {
  allQuestions,
  applicableQuestions,
  buildSubmission,
  deriveStage,
  loadCatalog,
  tiersById,
  validateAnswers,
} from '../src/designpartner/index.js';

const FIXTURES = join(
  dirname(fileURLToPath(import.meta.url)),
  '..', '..', '..', 'shared', 'designpartner', 'fixtures',
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

function load(inp: Record<string, any>, key: string): any {
  if (key in inp) return inp[key];
  return JSON.parse(readFileSync(join(FIXTURES, inp[`${key}_file`]), 'utf-8'));
}

/** Structural equality + serialized equality (pins key order). */
function expectFrozen(got: unknown, expected: unknown): void {
  expect(got).toEqual(expected);
  expect(JSON.stringify(got, null, 2)).toBe(JSON.stringify(expected, null, 2));
}

const skipAll = !existsSync(FIXTURES);

describe.skipIf(skipAll)('designpartner catalog fixtures', () => {
  it.each(cases('catalog').map((c) => [c.name, c] as const))('%s', (_name, c) => {
    const catalog = loadCatalog();
    const answers = load(c.input, 'answers');
    const applicable = applicableQuestions(catalog, answers);
    const applicableSet = new Set(applicable);
    const required = allQuestions(catalog)
      .filter((q) => q.required && applicableSet.has(q.id as string))
      .map((q) => q.id as string);
    expectFrozen({ applicable, required }, c.expected);
  });
});

describe.skipIf(skipAll)('designpartner validate fixtures', () => {
  it.each(cases('validate').map((c) => [c.name, c] as const))('%s', (_name, c) => {
    expectFrozen(validateAnswers(loadCatalog(), load(c.input, 'answers')), c.expected);
  });
});

describe.skipIf(skipAll)('designpartner payload fixtures', () => {
  it.each(cases('payload').map((c) => [c.name, c] as const))('%s', (_name, c) => {
    const inp = c.input;
    let diagnoseDocs = null;
    if ('diagnose_docs' in inp || 'diagnose_docs_file' in inp) {
      diagnoseDocs = load(inp, 'diagnose_docs');
    }
    const result = buildSubmission(
      { answers: load(inp, 'answers'), consent: inp.consent },
      diagnoseDocs,
      {
        version: inp.version ?? '0.0.0',
        submittedAt: inp.submitted_at ?? null,
        confirmedAt: inp.confirmed_at ?? null,
      },
    );
    expectFrozen(result, c.expected);
  });
});

describe.skipIf(skipAll)('designpartner state fixtures', () => {
  it.each(cases('state').map((c) => [c.name, c] as const))('%s', (_name, c) => {
    const stage = deriveStage(
      c.input.state,
      c.input.has_diagnose_run,
      tiersById(loadCatalog()),
    );
    expectFrozen({ stage }, c.expected);
  });
});
