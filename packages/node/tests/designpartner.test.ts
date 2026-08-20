/**
 * Unit tests for the TS designpartner engine — mirrors
 * test_designpartner_unit.py. House style: no mock libraries — the
 * transport takes an injected fetchFn, plus one test stubbing
 * globalThis.fetch by plain assignment with afterEach restore
 * (the cachelint-hook.test.ts pattern).
 */

import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, describe, expect, it } from 'vitest';

import { advance, sendSubmission } from '../src/designpartner/index.js';

const OPTS = { now: '2026-08-17T00:00:00Z', stamp: 't1', version: '9.9.9',
  url: 'http://example.invalid/api' };

const GOOD_ANSWERS: Record<string, unknown> = {
  name: 'Dana', email: 'dana@example.com',
  providers: ['openai'], models_openai: 'gpt-4o',
  calls_per_day: 'under_1k', monthly_spend: 'under_100',
  deployment_stage: 'exploring', model_choice_process: 'defaults',
  cache_effort: 'not_considered', biggest_pain: 'cost',
  features_interest: ['routing'], follow_up_call: false,
};

const noDiagnose = () => null;

describe('designpartner transport', () => {
  it('sends the exact body with the spec headers via the injected fetchFn', async () => {
    const calls: Array<{ url: string; init: RequestInit }> = [];
    const fetchFn = (async (url: any, init: any) => {
      calls.push({ url: String(url), init });
      return new Response('ok', { status: 200 });
    }) as typeof fetch;

    const ok = await sendSubmission('http://x/api', '{"a":1}', '9.9.9', { fetchFn });
    expect(ok).toBe(true);
    expect(calls[0].url).toBe('http://x/api');
    expect(calls[0].init.method).toBe('POST');
    expect(calls[0].init.body).toBe('{"a":1}');
    expect((calls[0].init.headers as Record<string, string>)['User-Agent']).toBe('tryaii/9.9.9');
    expect((calls[0].init.headers as Record<string, string>)['Content-Type']).toBe(
      'application/json',
    );
  });

  it.each([
    ['non-2xx', (async () => new Response('no', { status: 500 })) as typeof fetch],
    ['redirect-status', (async () => new Response('', { status: 302 })) as typeof fetch],
    ['network error', (async () => { throw new TypeError('fetch failed'); }) as typeof fetch],
  ])('collapses every failure to false (%s)', async (_label, fetchFn) => {
    expect(await sendSubmission('http://x/api', '{}', '0.0.0', { fetchFn })).toBe(false);
  });

  it('uses globalThis.fetch by default (stub + restore, house style)', async () => {
    const realFetch = globalThis.fetch;
    try {
      globalThis.fetch = (async () => new Response('ok', { status: 201 })) as typeof fetch;
      expect(await sendSubmission('http://x/api', '{}', '0.0.0')).toBe(true);
    } finally {
      globalThis.fetch = realFetch;
    }
  });
});

describe('designpartner advance', () => {
  let dir: string | null = null;

  afterEach(() => {
    if (dir !== null) rmSync(dir, { recursive: true, force: true });
    dir = null;
  });

  function tempDp(): string {
    dir = mkdtempSync(join(tmpdir(), 'tryaii-dp-'));
    return join(dir, 'dp');
  }

  it('full flow: the POST body IS the saved submission bytes', async () => {
    const dp = tempDp();
    const sent: Array<{ url: string; body: string }> = [];
    const send = (url: string, body: string) => {
      sent.push({ url, body });
      return true;
    };

    await advance(dp, {}, OPTS, { loadLatestDiagnose: noDiagnose });
    await advance(dp, { answers: GOOD_ANSWERS }, OPTS, { loadLatestDiagnose: noDiagnose });
    await advance(dp, { consent: 'contact_only' }, OPTS, { loadLatestDiagnose: noDiagnose });
    const report = await advance(dp, { confirm: true }, OPTS, {
      loadLatestDiagnose: noDiagnose,
      send,
    });

    expect(report.stage).toBe('submitted');
    expect(report.submission.delivered).toBe(true);
    const saved = readFileSync(join(dp, 'submission-t1.json'), 'utf-8');
    expect(sent[0].body).toBe(saved);
    expect(sent[0].url).toBe(OPTS.url);
    const doc = JSON.parse(saved);
    expect(doc.consent.confirmed_at).toBe(OPTS.now);
    expect(doc.diagnose).toBeNull();
  });

  it('insight tiers attach the right diagnose layers to the preview', async () => {
    const dp = tempDp();
    const docs = {
      run_id: 'r1',
      summary: { site_count: 1 },
      findings: { version: 1 },
      inventory: { sites: [] },
    };
    const seams = { loadLatestDiagnose: () => docs };

    await advance(dp, { answers: GOOD_ANSWERS }, OPTS, seams);
    await advance(dp, { consent: 'summary_insights' }, OPTS, seams);
    let preview = JSON.parse(readFileSync(join(dp, 'preview.json'), 'utf-8'));
    expect(preview.diagnose).toEqual({ run_id: 'r1', summary: { site_count: 1 } });

    await advance(dp, { consent: 'full_partnership' }, OPTS, seams);
    preview = JSON.parse(readFileSync(join(dp, 'preview.json'), 'utf-8'));
    expect(preview.diagnose.inventory).toEqual({ sites: [] });
  });

  it('confirm outside the confirm stage throws', async () => {
    const dp = tempDp();
    await advance(dp, {}, OPTS, { loadLatestDiagnose: noDiagnose });
    await expect(
      advance(dp, { confirm: true }, OPTS, { loadLatestDiagnose: noDiagnose }),
    ).rejects.toThrowError('nothing to confirm');
  });

  it('confirm rejects a tampered (stale) preview', async () => {
    const dp = tempDp();
    await advance(dp, { answers: GOOD_ANSWERS }, OPTS, { loadLatestDiagnose: noDiagnose });
    await advance(dp, { consent: 'contact_only' }, OPTS, { loadLatestDiagnose: noDiagnose });
    const previewPath = join(dp, 'preview.json');
    const doc = JSON.parse(readFileSync(previewPath, 'utf-8'));
    doc.answers.name = 'Somebody Else';
    writeFileSync(previewPath, JSON.stringify(doc, null, 2) + '\n', 'utf-8');
    await expect(
      advance(dp, { confirm: true }, OPTS, {
        loadLatestDiagnose: noDiagnose,
        send: () => true,
      }),
    ).rejects.toThrowError('preview is stale');
  });

  it('a new consent cycle clears the stored submission but keeps the file', async () => {
    const dp = tempDp();
    const seams = { loadLatestDiagnose: noDiagnose, send: () => true };
    await advance(dp, { answers: GOOD_ANSWERS }, OPTS, seams);
    await advance(dp, { consent: 'contact_only' }, OPTS, seams);
    await advance(dp, { confirm: true }, OPTS, seams);
    const report = await advance(dp, { consent: 'contact_only' }, OPTS, seams);
    expect(report.stage).toBe('confirm');
    expect(readFileSync(join(dp, 'submission-t1.json'), 'utf-8')).toBeTruthy();
  });

  it('reset removes state + preview, keeps submission records', async () => {
    const dp = tempDp();
    const seams = { loadLatestDiagnose: noDiagnose, send: () => false };
    await advance(dp, { answers: GOOD_ANSWERS }, OPTS, seams);
    await advance(dp, { consent: 'contact_only' }, OPTS, seams);
    await advance(dp, { confirm: true }, OPTS, seams);
    const report = await advance(dp, { reset: true }, OPTS, seams);
    expect(report.action.type).toBe('reset');
    expect(() => readFileSync(join(dp, 'state.json'))).toThrow();
    expect(readFileSync(join(dp, 'submission-t1.json'), 'utf-8')).toBeTruthy();
  });
});
