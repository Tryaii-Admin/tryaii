/**
 * Unit tests for the cachelint parity utilities (SPEC.md §1).
 *
 * Every expected value here was verified against the Python reference
 * (round(), f-strings, len(), json.dumps, _iso8601.parse_ts_ms) — these
 * utilities exist precisely because the JS built-ins disagree with Python
 * (Math.round rounds halves up, string indexing is UTF-16, Date.parse is
 * local-time). Mirrors packages/python/tests/test_cachelint_unit.py.
 */

import { describe, expect, it } from 'vitest';

import { formatFixed, halfEvenRound } from '../src/cachelint/util/halfEven.js';
import { formatThousands } from '../src/cachelint/util/pyformat.js';
import { CpIndex, cpLcp, cpLength } from '../src/cachelint/util/codepoints.js';
import { sortedStringify } from '../src/cachelint/util/stableJson.js';
import { parseTsMs } from '../src/cachelint/util/iso8601.js';

describe('halfEvenRound', () => {
  it('rounds exact halves to even (Python round())', () => {
    expect(halfEvenRound(2.5)).toBe(2);
    expect(halfEvenRound(3.5)).toBe(4);
    expect(halfEvenRound(0.5)).toBe(0);
    expect(halfEvenRound(1.5)).toBe(2);
    expect(halfEvenRound(99.5)).toBe(100);
  });

  it('rounds to one decimal like Python round(x, 1)', () => {
    expect(halfEvenRound(12.25, 1)).toBe(12.2);
    expect(halfEvenRound(240.25, 1)).toBe(240.2);
    expect(halfEvenRound(-12.25, 1)).toBe(-12.2);
    expect(halfEvenRound(12.2, 1)).toBe(12.2);
  });

  it('rounds the BINARY value, not the shortest decimal repr', () => {
    // 2.675 is stored as 2.67499...; Python round(2.675, 2) == 2.67.
    expect(halfEvenRound(2.675, 2)).toBe(2.67);
    // 12.35 is stored as 12.3499...; Python round(12.35, 1) == 12.3.
    expect(halfEvenRound(12.35, 1)).toBe(12.3);
    expect(halfEvenRound(0.125, 2)).toBe(0.12);
  });

  it('passes integers and non-ties through', () => {
    expect(halfEvenRound(600)).toBe(600);
    expect(halfEvenRound(2.4)).toBe(2);
    expect(halfEvenRound(2.6)).toBe(3);
  });
});

describe('formatFixed', () => {
  it("matches Python f'{x:.0f}'", () => {
    expect(formatFixed(2.5, 0)).toBe('2');
    expect(formatFixed(99.5, 0)).toBe('100');
    expect(formatFixed(12.25, 0)).toBe('12');
    expect(formatFixed(0.5, 0)).toBe('0');
    expect(formatFixed(3600, 0)).toBe('3600');
  });

  it("matches Python f'{x:.1f}' / '{x:.2f}'", () => {
    expect(formatFixed(12.25, 1)).toBe('12.2');
    expect(formatFixed(2.675, 2)).toBe('2.67');
  });
});

describe('formatThousands', () => {
  it("matches Python f'{n:,}'", () => {
    expect(formatThousands(0)).toBe('0');
    expect(formatThousands(512)).toBe('512');
    expect(formatThousands(1024)).toBe('1,024');
    expect(formatThousands(1234567)).toBe('1,234,567');
    expect(formatThousands(-4096)).toBe('-4,096');
  });
});

describe('code points', () => {
  it('cpLength counts code points like Python len()', () => {
    expect(cpLength('')).toBe(0);
    expect(cpLength('abc')).toBe(3);
    expect(cpLength('של')).toBe(2);
    expect(cpLength('😀😀')).toBe(2); // 4 UTF-16 units
  });

  it('CpIndex maps regex (UTF-16) indices to code points', () => {
    const s = '🚀🚀 id x'; // cp: 🚀=0 🚀=1 ' '=2 i=3 d=4 ' '=5 x=6
    const idx = new CpIndex(s);
    expect(idx.length).toBe(7);
    expect(idx.u16ToCp(0)).toBe(0);
    expect(idx.u16ToCp(4)).toBe(2); // after two surrogate pairs
    expect(idx.u16ToCp(8)).toBe(6); // where 'x' starts in UTF-16
    expect(idx.slice(3, 5)).toBe('id');
    expect(idx.slice(0, 1)).toBe('🚀');
    expect(idx.slice(5)).toBe(' x');
  });

  it('cpLcp counts common prefix in code points, never splitting a pair', () => {
    expect(cpLcp('abc', 'abd')).toBe(2);
    expect(cpLcp('', 'abc')).toBe(0);
    expect(cpLcp('same', 'same')).toBe(4);
    expect(cpLcp('a😀x', 'a😀y')).toBe(2);
    // 😀 (D83D DE00) vs 😁 (D83D DE01): shared high surrogate must NOT count.
    expect(cpLcp('a😀', 'a😁')).toBe(1);
  });
});

describe('sortedStringify', () => {
  it('matches the frozen canonical tools rendering (SPEC.md §1.4)', () => {
    // Same value as the can-tools-sorted-keys-integral-floats fixture core.
    expect(sortedStringify([{ zeta: 1.0, alpha: { b: 2.5, a: [1.0, 'x', 3] } }])).toBe(
      '[{"alpha":{"a":[1,"x",3],"b":2.5},"zeta":1}]',
    );
  });

  it('keeps non-ASCII verbatim and minifies', () => {
    expect(sortedStringify({ label: 'café' })).toBe('{"label":"café"}');
    expect(sortedStringify({})).toBe('{}');
    expect(sortedStringify([])).toBe('[]');
    expect(sortedStringify(null)).toBe('null');
  });

  it('sorts keys by code point, recursively', () => {
    expect(sortedStringify({ b: { d: 1, c: 2 }, a: 3 })).toBe('{"a":3,"b":{"c":2,"d":1}}');
  });
});

describe('parseTsMs (SPEC.md §1.6)', () => {
  it('parses epoch zero', () => {
    expect(parseTsMs('1970-01-01T00:00:00Z')).toBe(0);
  });

  it('agrees with UTC arithmetic for aware values', () => {
    const expected = Date.UTC(2026, 6, 30, 9, 0, 0); // test oracle only
    expect(parseTsMs('2026-07-30T09:00:00Z')).toBe(expected);
    expect(parseTsMs('2026-07-30T11:00:00+02:00')).toBe(expected);
    expect(parseTsMs('2026-07-30T11:34:00+0234')).toBe(expected);
    expect(parseTsMs('2026-07-30 09:00:00+00:00')).toBe(expected);
    expect(parseTsMs('2026-07-30t09:00:00z')).toBe(expected);
    // Naive = UTC (Date.parse would say local time — exactly why this exists).
    expect(parseTsMs('2026-07-30T09:00:00')).toBe(expected);
    expect(parseTsMs('2026-07-30T09:00')).toBe(expected);
  });

  it('truncates fractional seconds to milliseconds', () => {
    const base = parseTsMs('2026-07-30T09:00:00Z') as number;
    expect(parseTsMs('2026-07-30T09:00:00.123456789Z')).toBe(base + 123);
    expect(parseTsMs('2026-07-30T09:00:00.5Z')).toBe(base + 500);
    expect(parseTsMs('2026-07-30T09:00:00.25Z')).toBe(base + 250);
  });

  it('date-only means midnight UTC', () => {
    expect(parseTsMs('2026-07-30')).toBe(parseTsMs('2026-07-30T00:00:00Z'));
  });

  it('validates the calendar including leap days', () => {
    expect(parseTsMs('2024-02-29')).not.toBeNull();
    expect(parseTsMs('2026-02-29')).toBeNull();
  });

  it.each([
    [null],
    [''],
    ['yesterday-ish'],
    ['2026-13-01'],
    ['2026-00-10'],
    ['2026-01-32'],
    ['2026-07-30T24:00'],
    ['2026-07-30T09:60'],
    ['2026-07-30T09:00:61'],
    ['2026-7-30'],
    ['20260730T090000Z'],
    ['2026-07-30T09:00:00+25:00'],
    ['2026-07-30T09:00:00Z extra'],
    ['2026-07-30Z09:00'],
  ])('rejects %j', (value) => {
    expect(parseTsMs(value)).toBeNull();
  });
});
