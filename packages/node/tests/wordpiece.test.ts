/**
 * The Node WordPiece tokenizer produces exactly the token ids the Python SDK
 * feeds the int8 ONNX model (TS-253). Frozen by
 * shared/embeddings/tokenizer_parity.json (scripts/gen-embedding-fixtures.py):
 * the monitor fixture corpus, every starter training query, Unicode edge cases.
 */

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { WordPieceTokenizer, type TokenizerJson } from '../src/embeddings/wordpiece.js';

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, '..', '..', '..');
const tokenizerJson = JSON.parse(
  readFileSync(
    join(here, '..', 'src', 'embeddings', 'data', 'all-MiniLM-L6-v2-onnx-int8', 'tokenizer.json'),
    'utf-8',
  ),
) as TokenizerJson;
const fixture = JSON.parse(
  readFileSync(join(repo, 'shared', 'embeddings', 'tokenizer_parity.json'), 'utf-8'),
) as { max_length: number; cases: { source: string; text: string; ids: number[] }[] };

describe('WordPieceTokenizer (Python parity)', () => {
  const tok = new WordPieceTokenizer(tokenizerJson, fixture.max_length);

  it('covers the fixture corpus, the starter queries and the edge cases', () => {
    const sources = fixture.cases.map((c) => c.source.split('/')[0]);
    expect(new Set(sources)).toEqual(new Set(['monitor', 'starter', 'edge']));
    expect(fixture.cases.length).toBeGreaterThan(250);
  });

  for (const c of fixture.cases) {
    it(`${c.source}`, () => {
      const enc = tok.encode(c.text);
      expect(enc.ids).toEqual(c.ids);
      expect(enc.attentionMask).toEqual(c.ids.map(() => 1));
    });
  }
});
