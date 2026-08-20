/**
 * Run-store behavior (SPEC.md §4) — mirrors the store cases in
 * packages/python/tests/test_diagnose_unit.py.
 */

import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, describe, expect, it } from 'vitest';

import {
  latestRunId,
  listRunIds,
  loadRunFindings,
  previousRunId,
  writeRun,
} from '../src/diagnose/store.js';

function findings(runId: string): Record<string, unknown> {
  return {
    version: 1,
    run_id: runId,
    generated_at: '2026-01-01T00:00:00Z',
    tool: { name: 'tryaii', version: '0.0.0' },
  };
}

describe('diagnose store', () => {
  let dir: string | null = null;

  afterEach(() => {
    if (dir !== null) rmSync(dir, { recursive: true, force: true });
    dir = null;
  });

  function tempDir(): string {
    dir = mkdtempSync(join(tmpdir(), 'tryaii-diagnose-'));
    return dir;
  }

  it('round-trips runs and maintains the latest pointer', () => {
    const out = join(tempDir(), 'diagnose');
    const inventory = { sites: [{ file: 'a.py', line: 1, prompt: 'x' }] };

    const paths = writeRun(out, inventory, findings('20260101T000000Z'));
    writeRun(out, inventory, findings('20260102T000000Z'));

    // \n newlines on every platform (byte parity with the Python store)
    expect(readFileSync(paths.findings, 'utf-8')).not.toContain('\r\n');
    expect(JSON.parse(readFileSync(paths.inventory, 'utf-8'))).toEqual(inventory);

    expect(listRunIds(out)).toEqual(['20260101T000000Z', '20260102T000000Z']);
    expect(latestRunId(out)).toBe('20260102T000000Z');
    expect(previousRunId(out, '20260102T000000Z')).toBe('20260101T000000Z');
    expect(previousRunId(out, '20260101T000000Z')).toBeNull();
    expect(loadRunFindings(out, '20260101T000000Z').run_id).toBe('20260101T000000Z');
  });

  it('latest falls back to the newest run dir when the pointer dangles', () => {
    const out = join(tempDir(), 'diagnose');
    writeRun(out, {}, findings('20260101T000000Z'));
    writeFileSync(join(out, 'latest'), 'someday-run\n', 'utf-8');
    expect(latestRunId(out)).toBe('20260101T000000Z');
  });

  it('handles a missing store dir', () => {
    const out = join(tempDir(), 'missing');
    expect(listRunIds(out)).toEqual([]);
    expect(latestRunId(out)).toBeNull();
  });
});
