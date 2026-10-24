/**
 * Cache fields of the event (API.md v1.4 §3.2.1, SPEC.md §3.1). Port of
 * packages/python/tests/test_monitor_cache.py.
 *
 * The pure helpers are pinned cross-SDK by shared/monitor/fixtures/cache
 * (monitor-fixtures.test.ts); this file covers the wrapper wiring: where the
 * block sits in the event, when it is absent, and that the local events file
 * never holds prompt text.
 */

import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import * as engine from '../src/cachelint/index.js';
import {
  CacheTracker,
  Monitor,
  clock,
  lintPrompt,
  store,
  usageFromResponse,
  wrap,
} from '../src/monitor/index.js';

const NOW = Date.UTC(2026, 9, 11, 12, 0, 0, 123);
const STATIC = 'Refund policy: items may be returned within thirty days of delivery. '.repeat(110);
const SECRET_DATE = '2026-10-18 14:02';
const SYSTEM = `You are the support assistant. Now: ${SECRET_DATE}. Customer {{sku}}. ` + STATIC;
const SCORES = [{ modelId: 'openai/gpt-5-mini', qualityScore: 0.8 }];

function messages(user = 'Where is my order?') {
  return [
    { role: 'system', content: SYSTEM },
    { role: 'user', content: user },
  ];
}

class FakeCompletions {
  calls: Record<string, unknown>[] = [];
  constructor(private readonly usage: Record<string, unknown>) {}
  create(body: Record<string, unknown>): Promise<unknown> {
    this.calls.push({ ...body });
    return Promise.resolve({ model: body.model, usage: this.usage });
  }
}

function client(usage: Record<string, unknown> = { prompt_tokens: 100, completion_tokens: 5, total_tokens: 105 }) {
  return { baseURL: 'https://api.openai.com/v1', chat: { completions: new FakeCompletions(usage) } };
}

const routeFn = () => [SCORES, null, null] as [unknown[], unknown, unknown];

function eventsOf(monitor: Monitor): Record<string, any>[] {
  return store.readRecentEvents(monitor.dir, 50);
}

let dataDir: string;
const savedNow = clock.now;
const savedDir = process.env.TRYAII_DRE_DATA_DIR;

beforeEach(() => {
  dataDir = mkdtempSync(join(tmpdir(), 'tryaii-monitor-cache-'));
  process.env.TRYAII_DRE_DATA_DIR = dataDir;
  clock.now = () => NOW;
});

afterEach(() => {
  clock.now = savedNow;
  if (savedDir === undefined) delete process.env.TRYAII_DRE_DATA_DIR;
  else process.env.TRYAII_DRE_DATA_DIR = savedDir;
  rmSync(dataDir, { recursive: true, force: true });
});

describe('cache fields', () => {
  it('records cached_tokens without cachelint and no cache block', async () => {
    const c = client({
      prompt_tokens: 1400,
      completion_tokens: 10,
      total_tokens: 1410,
      prompt_tokens_details: { cached_tokens: 1280 },
    });
    const monitor = wrap(c, { app: 'shop', process: 'support', routeFn });
    await c.chat.completions.create({ model: 'gpt-5.5', messages: messages() });
    await monitor.drain();
    const [event] = eventsOf(monitor);
    expect(event.usage).toEqual({
      prompt_tokens: 1400, completion_tokens: 10, total_tokens: 1410,
      cached_tokens: 1280, cache_write_tokens: null, cache_writes: null, reasoning_tokens: null,
    });
    expect('cache' in event).toBe(false);
  });

  it('puts the cache block last and never writes prompt text', async () => {
    const c = client();
    const monitor = wrap(c, { app: 'shop', process: 'support', routeFn, cachelint: true });
    await c.chat.completions.create({ model: 'gpt-4o-mini', messages: messages() });
    await monitor.drain();
    await c.chat.completions.create({ model: 'gpt-4o-mini', messages: messages('Can I return it?') });
    await monitor.drain();
    const events = eventsOf(monitor);
    expect(events).toHaveLength(2);
    for (const e of events) expect(Object.keys(e).slice(-2)).toEqual(['vector', 'cache']);
    const [first, second] = events.map((e) => e.cache);
    expect(first.outcome).toBeNull();
    expect(second.report).toBeNull();
    expect(second.outcome).toBe('partial');
    expect(first.report.verdict).toBe('EFFECTIVELY_UNCACHEABLE');
    expect(first.report.findings.map((f: { excerpt: string }) => f.excerpt)).toEqual(['0000-00-00 00:00', '{{xxx}}']);
    expect(first.report.recommendation).toMatch(/^at messages\[0\]:system\+/);
    const raw = store.eventFiles(monitor.dir).map((p) => readFileSync(p, 'utf-8')).join('');
    for (const secret of [SECRET_DATE, '{{sku}}', 'Refund policy', 'Where is my order']) {
      expect(raw.includes(secret)).toBe(false);
    }
  });

  it('snapshots the messages the caller may mutate afterwards', async () => {
    const c = client();
    const monitor = wrap(c, { app: 'shop', process: 'support', routeFn, cachelint: true });
    const msgs = messages();
    const pending = c.chat.completions.create({ model: 'gpt-4o-mini', messages: msgs });
    msgs[0].content = 'changed';
    msgs.push({ role: 'assistant', content: 'later' });
    await pending;
    await monitor.drain();
    const [event] = eventsOf(monitor);
    expect(event.cache.report.findings).toHaveLength(2); // the original system prompt was linted
  });

  it('keeps the report cadence per process', () => {
    let t = 0;
    const tracker = new CacheTracker(engine, () => t);
    const a = tracker.observe('p', 'gpt-4o-mini', messages());
    expect(a.outcome).toBeNull();
    expect(a.report).not.toBeNull();
    t = 30_000;
    const b = tracker.observe('p', 'gpt-4o-mini', messages('Another question'));
    expect(b.report).toBeNull();
    t = 120_000;
    const later = tracker.observe('p', 'gpt-4o-mini', messages('Another question'));
    expect(later.report).not.toBeNull();
    t = 300_000;
    expect(tracker.observe('p', 'gpt-4o-mini', messages('Another question')).report).toBeNull();
    const other = tracker.observe('q', 'gpt-4o-mini', messages());
    expect(other.outcome).toBeNull();
    expect(other.report).not.toBeNull();
  });

  it('runs the full analysis at most once per 60 s per process', () => {
    // A per-call timestamp changes the prompt every call while the masked
    // report stays the same: 20 calls 10 s apart must not analyse every call.
    let t = 0;
    const tracker = new CacheTracker(engine, () => t);
    let reports = 0;
    for (let i = 0; i < 20; i++) {
      t = i * 10_000;
      const stamp = `2026-10-18 14:${String(2 + Math.floor(i / 6)).padStart(2, '0')}:${String((i * 10) % 60).padStart(2, '0')}`;
      const block = tracker.observe('p', 'gpt-4o-mini', [
        { role: 'system', content: SYSTEM.replace(SECRET_DATE, stamp) },
        { role: 'user', content: 'Can I return shoes?' },
      ]);
      if (block.report !== null) reports++;
      if (i > 0) expect(block.outcome).toBe('miss'); // the pair prediction runs every call
    }
    expect(tracker.analyses).toBe(4); // t = 0, 60, 120, 180 s
    expect(reports).toBe(1);
    tracker.observe('q', 'gpt-4o-mini', messages());
    expect(tracker.analyses).toBe(5); // per process
  });

  const TOOLS = [
    {
      type: 'function',
      function: {
        name: 'lookup_order',
        description: 'Look up an order by its id.',
        parameters: { type: 'object', properties: { order_id: { type: 'string' } }, required: ['order_id'] },
      },
    },
  ];
  const FUNCTIONS = [{ name: 'escalate', parameters: { type: 'object', properties: {} } }];

  it('lints tools then legacy functions', () => {
    expect(lintPrompt([])).toEqual({ messages: [] });
    expect(lintPrompt([], TOOLS, FUNCTIONS)).toEqual({ messages: [], tools: [...TOOLS, ...FUNCTIONS] });
    expect(lintPrompt([], undefined, FUNCTIONS)).toEqual({ messages: [], tools: FUNCTIONS });
    expect(lintPrompt([], 'bad', [])).toEqual({ messages: [] });
  });

  it('passes the request tool schemas to the lint', async () => {
    const c = client();
    const monitor = wrap(c, { app: 'shop', process: 'support', routeFn, cachelint: true });
    await c.chat.completions.create({ model: 'gpt-4o-mini', messages: messages(), tools: TOOLS, functions: FUNCTIONS });
    await monitor.drain();
    const [event] = eventsOf(monitor);
    const report = event.cache.report;
    expect(report.tools_tokens).toBeGreaterThan(0);
    expect(report.prompt_tokens).toBe(report.tools_tokens + report.system_tokens + report.messages_tokens);
    const raw = store.eventFiles(monitor.dir).map((p) => readFileSync(p, 'utf-8')).join('');
    expect(raw.includes('lookup_order')).toBe(false);
  });

  it('reads cached tokens from object usage', () => {
    expect(
      usageFromResponse({ prompt_tokens: 100, completion_tokens: 5, total_tokens: 105, cache_read_input_tokens: 64 }),
    ).toEqual({
      prompt_tokens: 100, completion_tokens: 5, total_tokens: 105,
      cached_tokens: 64, cache_write_tokens: null, cache_writes: null, reasoning_tokens: null,
    });
  });
});
