/**
 * The Unigram tokenizer (src/embeddings/unigram.ts) against HF `tokenizers`.
 *
 * shared/embeddings/unigram_parity.json (scripts/gen-unigram-fixture.py)
 * freezes the Python ids for the H4 router's tokenizer.json; every case must
 * match exactly. The tokenizer is read from packages/router-h4-model (or
 * TRYAII_H4_TOKENIZER) and must be the file the fixture was made from.
 */

import { createHash } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import {
  UnigramTokenizer,
  UnigramTokenizerError,
  nfkc,
  sliceCodePoints,
  translateRegex,
} from '../src/embeddings/unigram.js';

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, '..', '..', '..');
const fixture = JSON.parse(
  readFileSync(join(repo, 'shared', 'embeddings', 'unigram_parity.json'), 'utf-8'),
) as { tokenizer_sha256: string; count: number; cases: { cat: string; text: string; ids: number[] }[] };

const tokenizerPath =
  process.env.TRYAII_H4_TOKENIZER ?? join(repo, 'packages', 'router-h4-model', 'tokenizer.json');
const raw = existsSync(tokenizerPath) ? readFileSync(tokenizerPath) : null;
const tokJson = raw ? (JSON.parse(raw.toString('utf-8')) as Record<string, unknown>) : null;

/** A tiny valid tokenizer.json for the error and unit cases. */
function mini(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    added_tokens: [{ id: 0, content: '<unk>', normalized: false, special: true, lstrip: false, rstrip: false, single_word: false }],
    normalizer: { type: 'Sequence', normalizers: [{ type: 'NFKC' }, { type: 'Strip', strip_left: true, strip_right: true }] },
    pre_tokenizer: { type: 'Metaspace', replacement: '▁', prepend_scheme: 'always', split: false },
    post_processor: null,
    model: {
      type: 'Unigram',
      unk_id: 0,
      byte_fallback: false,
      vocab: [['<unk>', 0], ['▁', -2], ['a', -3], ['b', -3], ['ab', -4], ['▁ab', -5], ['\u{1F600}', -1]],
    },
    ...overrides,
  };
}

describe.skipIf(tokJson === null)('Unigram parity with HF tokenizers (H4)', () => {
  const tok = new UnigramTokenizer(tokJson);

  it('tokenizer.json is the file the fixture was generated from', () => {
    const sha = createHash('sha256').update(raw as Buffer).digest('hex');
    expect(sha, `regenerate with scripts/gen-unigram-fixture.py (${tokenizerPath})`).toBe(fixture.tokenizer_sha256);
    expect(fixture.cases.length).toBe(fixture.count);
    expect(fixture.count).toBeGreaterThanOrEqual(2500);
  });

  it('matches every fixture case exactly', () => {
    const failures: string[] = [];
    for (const c of fixture.cases) {
      const got = tok.encode(c.text);
      if (got.length !== c.ids.length || got.some((v, i) => v !== c.ids[i])) {
        failures.push(`${c.cat} ${JSON.stringify(c.text.slice(0, 60))}: got ${got.slice(0, 20)} want ${c.ids.slice(0, 20)}`);
      }
    }
    expect(failures.slice(0, 20), `${failures.length}/${fixture.cases.length} mismatches`).toEqual([]);
  });

  it('encodes ~1k tokens in about a millisecond', () => {
    const long = fixture.cases.filter((c) => c.cat === 'hebrew' || c.cat === 'english' || c.cat === 'mixed');
    let text = '';
    let tokens = 0;
    for (const c of long) {
      if (tokens >= 1000) break;
      text += `${c.text} `;
      tokens += c.ids.length;
    }
    for (let i = 0; i < 20; i++) tok.encode(text); // warm up
    const runs = 200;
    const t0 = performance.now();
    for (let i = 0; i < runs; i++) tok.encode(text);
    const perCall = (performance.now() - t0) / runs;
    expect(tok.encode(text).length).toBeGreaterThanOrEqual(1000);
    // Budget 1 ms on one core; allow headroom for loaded CI machines.
    expect(perCall).toBeLessThan(5);
  });

  it('stays linear on huge inputs and caps them', () => {
    const word = 'א'.repeat(100_000);
    const t0 = performance.now();
    const ids = tok.encode(word);
    expect(performance.now() - t0).toBeLessThan(2000);
    expect(ids.length).toBeGreaterThan(0);
    const capped = new UnigramTokenizer(tokJson, { maxInputLength: 10 });
    expect(capped.encode('abcdefghij'.repeat(100))).toEqual(tok.encode('abcdefghij'));
    // never cuts a surrogate pair in half
    expect(capped.encode('aaaaaaaaa\u{1F600}')).toEqual(tok.encode('aaaaaaaaa'));
  });

  it('replaces lone surrogates instead of crashing', () => {
    expect(tok.encode('a\uD800b')).toEqual(tok.encode('a�b'));
  });
});

describe('Unigram tokenizer units', () => {
  it('Viterbi prefers the best total score and fuses unknowns', () => {
    const t = new UnigramTokenizer(mini());
    expect(t.encode('ab')).toEqual([5]); // ▁ab (-5) beats ▁ + ab (-6)
    expect(t.encode('ba')).toEqual([1, 3, 2]);
    expect(t.encode('  xyz ')).toEqual([1, 0]); // ▁ then one fused unk
    expect(t.encode('\u{1F600}')).toEqual([1, 6]);
    expect(t.encode('')).toEqual([]);
    expect(t.encode('   ')).toEqual([]);
    expect(t.encode('a<unk>b')).toEqual([1, 2, 0, 1, 3]);
  });

  it('NFKC guard keeps code points Rust does not know', () => {
    expect(nfkc('ꟲ')).toBe('ꟲ');
    expect(nfkc('\u{1CCD6}')).toBe('\u{1CCD6}');
    expect(nfkc('\u{105D2}̇')).toBe('\u{105D2}̇');
    expect(nfkc('ﬁ①')).toBe('fi1');
  });

  it('translates only the safe regex subset', () => {
    expect(new RegExp(translateRegex(' {2,}'), 'gu').test('a  b')).toBe(true);
    expect(new RegExp(translateRegex('\\s+'), 'u').test('\u0085')).toBe(true);
    expect(new RegExp(translateRegex('\\s+'), 'u').test('﻿')).toBe(false);
    expect(new RegExp(translateRegex('[\u0001-\u0003▁]'), 'u').test('\u0002')).toBe(true);
    for (const bad of ['a|b', '(a)', '\\d', '[^a]', '.', '[a\\]]', '']) {
      expect(() => translateRegex(bad), bad).toThrow(UnigramTokenizerError);
    }
  });

  it('slices by code points', () => {
    expect(sliceCodePoints('a\u{1F600}b', 2)).toBe('a\u{1F600}');
    expect(sliceCodePoints('abc', 5)).toBe('abc');
  });

  const errorCases: [string, unknown, 'malformed' | 'unsupported'][] = [
    ['not an object', 42, 'malformed'],
    ['no model', { ...mini(), model: undefined }, 'malformed'],
    ['BPE model', mini({ model: { type: 'BPE', vocab: {} } }), 'unsupported'],
    ['empty vocab', mini({ model: { type: 'Unigram', unk_id: 0, vocab: [] } }), 'malformed'],
    ['bad vocab entry', mini({ model: { type: 'Unigram', unk_id: 0, vocab: [['a', 'x']] } }), 'malformed'],
    ['non-finite score', mini({ model: { type: 'Unigram', unk_id: 0, vocab: [['a', null]] } }), 'malformed'],
    ['unk out of range', mini({ model: { type: 'Unigram', unk_id: 9, vocab: [['a', -1]] } }), 'malformed'],
    ['byte fallback', mini({ model: { type: 'Unigram', unk_id: 0, byte_fallback: true, vocab: [['a', -1]] } }), 'unsupported'],
    ['Precompiled normalizer', mini({ normalizer: { type: 'Precompiled', precompiled_charsmap: 'AA==' } }), 'unsupported'],
    ['Lowercase normalizer', mini({ normalizer: { type: 'Lowercase' } }), 'unsupported'],
    ['bad Replace', mini({ normalizer: { type: 'Replace', pattern: {}, content: ' ' } }), 'malformed'],
    ['empty-matching regex', mini({ normalizer: { type: 'Replace', pattern: { Regex: 'a*' }, content: '' } }), 'unsupported'],
    ['ByteLevel pre-tokenizer', mini({ pre_tokenizer: { type: 'ByteLevel' } }), 'unsupported'],
    ['no pre-tokenizer', mini({ pre_tokenizer: null }), 'unsupported'],
    ['Metaspace split', mini({ pre_tokenizer: { type: 'Metaspace', replacement: '▁', prepend_scheme: 'always', split: true } }), 'unsupported'],
    ['Metaspace first', mini({ pre_tokenizer: { type: 'Metaspace', replacement: '▁', prepend_scheme: 'first', split: false } }), 'unsupported'],
    ['special tokens in template', mini({ post_processor: { type: 'TemplateProcessing', single: [{ SpecialToken: { id: '<s>' } }, { Sequence: { id: 'A' } }] } }), 'unsupported'],
    ['normalized added token', mini({ added_tokens: [{ id: 0, content: 'x', normalized: true }] }), 'unsupported'],
  ];
  for (const [name, json, code] of errorCases) {
    it(`rejects ${name}`, () => {
      let err: unknown;
      try {
        new UnigramTokenizer(json);
      } catch (e) {
        err = e;
      }
      expect(err).toBeInstanceOf(UnigramTokenizerError);
      expect((err as UnigramTokenizerError).code).toBe(code);
    });
  }
});
