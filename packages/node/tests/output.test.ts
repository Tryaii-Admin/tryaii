import { afterEach, describe, expect, it } from 'vitest';

import { writePaced, type PacedStream } from '../src/output.js';

/**
 * Unit tests for the paced output helper. They exercise both branches (instant
 * dump vs. line-by-line reveal) with a fake stream and a zero delay, so the
 * pacing logic is covered without real timers or a real TTY.
 */

class FakeStream implements PacedStream {
  chunks: string[] = [];
  constructor(public isTTY: boolean) {}
  write(chunk: string): void {
    this.chunks.push(chunk);
  }
  get text(): string {
    return this.chunks.join('');
  }
}

const TEXT = 'line one\nline two\nline three';

describe('writePaced', () => {
  afterEach(() => {
    delete process.env.TRYAII_NO_BANNER;
  });

  it('dumps everything in one write when not a TTY', async () => {
    const s = new FakeStream(false);
    await writePaced(TEXT, s, 0);
    expect(s.chunks.length).toBe(1);
    expect(s.text).toBe(TEXT);
  });

  it('reveals line-by-line on a TTY, preserving exact content', async () => {
    const s = new FakeStream(true);
    await writePaced(TEXT, s, 0);
    // one write per line (3 lines)
    expect(s.chunks.length).toBe(3);
    // reassembled output is byte-identical to the input
    expect(s.text).toBe(TEXT);
    // only the non-final lines carry a trailing newline
    expect(s.chunks).toEqual(['line one\n', 'line two\n', 'line three']);
  });

  it('dumps instantly on a TTY when TRYAII_NO_BANNER is set', async () => {
    process.env.TRYAII_NO_BANNER = '1';
    const s = new FakeStream(true);
    await writePaced(TEXT, s, 0);
    expect(s.chunks.length).toBe(1);
    expect(s.text).toBe(TEXT);
  });

  it('handles single-line text without a trailing newline', async () => {
    const s = new FakeStream(true);
    await writePaced('just one line', s, 0);
    expect(s.chunks).toEqual(['just one line']);
  });
});
