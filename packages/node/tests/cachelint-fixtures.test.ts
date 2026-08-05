/**
 * Golden-fixture conformance for the TypeScript cachelint engine.
 *
 * The fixtures in shared/cachelint/fixtures/ were generated from the PYTHON
 * reference engine and frozen (SPEC.md §5) — this suite proves the TS port
 * reproduces every expected block byte-for-byte: structural equality via
 * toEqual PLUS JSON.stringify equality, which also pins key ORDER (the
 * --json CLI outputs are diffed directly across SDKs).
 *
 * The cli suite is exercised by packages/python/tests/test_cachelint_cli_parity.py
 * (spawns both built CLIs); it needs dist/ and is not run here.
 */

import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join, dirname } from 'node:path';

import { describe, expect, it } from 'vitest';

import { analyze, buildCanonical, renderReport, resolve, scan, firstBlocking, countTokens } from '../src/cachelint/index.js';
import { findingToDict } from '../src/cachelint/detectors.js';

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..', 'shared', 'cachelint', 'fixtures');

interface Case {
  name: string;
  description: string;
  input: Record<string, any>;
  expected: any;
  stderr_parser_specific?: boolean;
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

const skipAll = !existsSync(FIXTURES);

describe.skipIf(skipAll)('detectors fixtures', () => {
  it.each(cases('detectors').map((c) => [c.name, c] as const))('%s', (_name, c) => {
    const findings = scan(c.input.text);
    const first = firstBlocking(findings);
    expectFrozen(findings.map(findingToDict), c.expected.findings);
    const index = first !== null ? findings.indexOf(first) : null;
    expect(index).toBe(c.expected.first_blocking_index);
  });
});

describe.skipIf(skipAll)('resolve fixtures', () => {
  it.each(cases('resolve').map((c) => [c.name, c] as const))('%s', (_name, c) => {
    if ('error' in c.expected) {
      expect(() => resolve(c.input.provider, c.input.model)).toThrowError(c.expected.error);
      return;
    }
    const r = resolve(c.input.provider, c.input.model);
    expectFrozen(
      {
        provider_key: r.provider_key,
        min_tokens: r.min_tokens,
        tier_note: r.tier_note,
        warnings: r.warnings,
        upstream: r.upstream,
      },
      c.expected,
    );
  });
});

describe.skipIf(skipAll)('tokenize fixtures', () => {
  it.each(cases('tokenize').map((c) => [c.name, c] as const))('%s', (_name, c) => {
    const tc = countTokens(c.input.text, c.input.provider_key, c.input.upstream ?? null);
    expectFrozen(tc, c.expected);
  });
});

describe.skipIf(skipAll)('canonical fixtures', () => {
  it.each(cases('canonical').map((c) => [c.name, c] as const))('%s', (_name, c) => {
    const [text, sections, nonText] = buildCanonical(c.input.prompt);
    expect(text).toBe(c.expected.text);
    expectFrozen(sections, c.expected.sections);
    expect(nonText).toBe(c.expected.non_text);
  });
});

describe.skipIf(skipAll)('analyze fixtures', () => {
  it.each(cases('analyze').map((c) => [c.name, c] as const))('%s', (_name, c) => {
    expectFrozen(analyze(loadInputData(c.input)), c.expected);
  });
});

describe.skipIf(skipAll)('sequence fixtures', () => {
  it.each(cases('sequence').map((c) => [c.name, c] as const))('%s', (_name, c) => {
    expectFrozen(analyze(loadInputData(c.input)).sequences, c.expected);
  });
});

describe.skipIf(skipAll)('errors fixtures', () => {
  it.each(cases('errors').map((c) => [c.name, c] as const))('%s', (_name, c) => {
    expect(() => analyze(loadInputData(c.input))).toThrowError(c.expected.error);
  });
});

describe.skipIf(skipAll)('report fixtures', () => {
  it.each(cases('report').map((c) => [c.name, c] as const))('%s', (_name, c) => {
    const golden = readFileSync(join(FIXTURES, 'report', c.expected.golden), 'utf-8');
    const rendered = renderReport(analyze(loadInputData(c.input))) + '\n';
    expect(rendered).toBe(golden.replace(/\r\n/g, '\n'));
  });
});
