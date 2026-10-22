/**
 * The monitor store (SPEC.md §3): daily JSONL append, per-day vector
 * dedupe, window reads and atomic pins/config documents. Byte shapes mirror
 * monitor/store.py (minified `\n` lines, indent-2 documents).
 */

import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { starterBundle } from '../src/catalog/bundle.js';
import {
  CACHE_TTLS,
  buildEvent,
  buildVector,
  normalizeCacheUsage,
  normalizeUsage,
  emptyConfig,
  emptyPins,
  pickWinner,
  policyAllowed,
  resolveMode,
  routeAllowed,
  setPin,
  setProcessMode,
  shadowAllowed,
  sourceInfo,
  store,
  taxonomyVersion,
  uploadInfo,
} from '../src/monitor/index.js';
import { ModelPolicy } from '../src/policy.js';
import { ModelRegistry } from '../src/registry/models.js';

function event(ts: string, process = 'p', extra: Record<string, unknown> = {}): Record<string, unknown> {
  return buildEvent({
    ts,
    app: 'shop',
    process,
    call_site: { file: 'a.py', line: 1, function: 'f' },
    provider: 'openai',
    model_requested: 'gpt-5.5',
    model_resolved: 'openai/gpt-5.5',
    model_used: 'gpt-5.5',
    mode: 'off',
    winner: 'openai/gpt-5.4',
    winner_reachable: true,
    route_status: 'ok',
    classification: null,
    usage: { prompt_tokens: 10, completion_tokens: 2, total_tokens: 12 },
    est_cost_usd: 0.0001234567,
    est_cost_winner_usd: null,
    latency_ms: 12.9,
    stream: false,
    error: null,
    prompt_hash: 'h',
    vector: false,
    ...extra,
  } as any);
}

describe('monitor store', () => {
  let dir: string;
  beforeEach(() => {
    dir = join(mkdtempSync(join(tmpdir(), 'tryaii-monitor-')), 'monitor', 'shop');
  });
  afterEach(() => {
    rmSync(join(dir, '..', '..'), { recursive: true, force: true });
  });

  it('appends one minified line per event into the UTC day file', () => {
    const path = store.appendEvent(dir, event('2026-10-11T23:59:59.999Z'));
    store.appendEvent(dir, event('2026-10-12T00:00:00.000Z'));
    expect(path.endsWith('events-2026-10-11.jsonl')).toBe(true);
    const text = readFileSync(path, 'utf-8');
    expect(text.endsWith('\n')).toBe(true);
    expect(text.includes('\r')).toBe(false);
    expect(text.includes(': ')).toBe(false); // minified
    const parsed = JSON.parse(text.trim());
    expect(Object.keys(parsed)[0]).toBe('schema');
    expect(parsed.est_cost_usd).toBe(0.000123); // 6 dp half-even
    expect(parsed.latency_ms).toBe(12);
    expect(store.readLines(path)).toHaveLength(1);
  });

  it('reads events within the window across day files, append order', () => {
    store.appendEvent(dir, event('2026-10-09T12:00:00.000Z'));
    store.appendEvent(dir, event('2026-10-10T13:00:00.000Z'));
    store.appendEvent(dir, event('2026-10-11T11:00:00.000Z'));
    store.appendEvent(dir, event('2026-10-11T12:00:00.001Z')); // after now
    const got = store.readEvents(dir, '2026-10-11T12:00:00.000Z', 24 * 3600);
    expect(got.map((e) => e.ts)).toEqual(['2026-10-10T13:00:00.000Z', '2026-10-11T11:00:00.000Z']);
    expect(store.readRecentEvents(dir, 3).map((e) => e.ts)).toEqual([
      '2026-10-10T13:00:00.000Z',
      '2026-10-11T11:00:00.000Z',
      '2026-10-11T12:00:00.001Z',
    ]);
    expect(store.readEvents(join(dir, 'missing'), '2026-10-11T12:00:00.000Z', 60)).toEqual([]);
  });

  it('writes a vector once per prompt hash per day, seeded from disk', () => {
    const v = (ts: string, hash: string) =>
      buildVector({ prompt_hash: hash, ts, process: 'p', model: 'm', vector: [0.123456, 1, -0.00005] });
    expect(store.appendVector(dir, v('2026-10-11T10:00:00.000Z', 'a'))).toBe(true);
    expect(store.appendVector(dir, v('2026-10-11T11:00:00.000Z', 'a'))).toBe(false);
    expect(store.appendVector(dir, v('2026-10-12T11:00:00.000Z', 'a'))).toBe(true);
    const line = readFileSync(store.vectorsPath(dir, '2026-10-11T10:00:00.000Z'), 'utf-8');
    expect(line).toBe(
      '{"schema":"tryaii.monitor.vector/1","prompt_hash":"a","ts":"2026-10-11T10:00:00.000Z","process":"p","model":"m","dim":3,"v":[0.1235,1,0]}\n',
    );
  });

  it('round-trips pins and config as indent-2 documents', () => {
    expect(store.readPins(dir)).toEqual(emptyPins());
    expect(store.readConfig(dir)).toEqual(emptyConfig());
    const pins = setPin(emptyPins(), 'p', {
      model: 'openai/gpt-5.4',
      pinned_at: '2026-10-11T12:00:00.000Z',
      expires_at: '2026-10-12T12:00:00.000Z',
      basis: { window: 'first_call', events: 1, counts: { 'openai/gpt-5.4': 1 } },
    });
    const pinsPath = store.writePins(dir, pins);
    expect(readFileSync(pinsPath, 'utf-8')).toBe(JSON.stringify(pins, null, 2) + '\n');
    expect(store.readPins(dir)).toEqual(pins);
    const config = setProcessMode(null, 'p', 'stable');
    store.writeConfig(dir, config);
    expect(store.readConfig(dir)).toEqual(config);
    expect(store.configMtime(dir)).not.toBeNull();
    expect(store.listApps(join(dir, '..', '..'))).toEqual(['shop']);
  });
});

// --- slice 2: event ids, taxonomy, managed, policy, remote, upload ----------

describe('slice 2 engine', () => {
  let dir: string;
  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'tryaii-monitor-s2-'));
  });
  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it('builds v2 events in contract key order with a fresh uuid4 unless given', () => {
    const a = event('2026-10-11T12:00:00.000Z');
    const b = event('2026-10-11T12:00:00.000Z');
    expect(Object.keys(a)).toEqual([
      'schema', 'event_id', 'ts', 'app', 'process', 'call_site', 'provider',
      'model_requested', 'model_resolved', 'model_used', 'mode', 'winner',
      'winner_reachable', 'route_status', 'classification', 'taxonomy_version', 'usage',
      'cache_ttl_policy',
      'est_cost_usd', 'est_cost_winner_usd', 'est_cost_anchor_usd', 'latency_ms',
      'stream', 'error', 'prompt_hash', 'vector',
    ]);
    expect(a.cache_ttl_policy).toBeNull();
    expect(a.usage).toEqual({
      prompt_tokens: 10, completion_tokens: 2, total_tokens: 12,
      cached_tokens: null, cache_write_tokens: null, cache_writes: null,
    });
    expect(a.event_id).not.toBe(b.event_id);
    expect(a.event_id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    expect(event('2026-10-11T12:00:00.000Z', 'p', { event_id: 'e-1' }).event_id).toBe('e-1');
    expect(event('2026-10-11T12:00:00.000Z', 'p', { taxonomy_version: '2026.10' }).taxonomy_version).toBe('2026.10');
    expect(event('2026-10-11T12:00:00.000Z', 'p', { est_cost_anchor_usd: 0.0012344999 }).est_cost_anchor_usd).toBe(0.001234);
  });

  it('carries prompt-cache usage in contract order (SPEC.md §3.1b, TS-228)', () => {
    const usage = {
      total_tokens: 12,
      cache_writes: [{ ttl: '1H', tokens: 5.0 }, { ttl: '5m', tokens: 0 }],
      prompt_tokens: 10,
      cached_tokens: true,
      completion_tokens: 2,
    };
    const e = event('2026-10-11T12:00:00.000Z', 'p', { usage, cache_ttl_policy: '1h' });
    expect(Object.keys(e.usage as object)).toEqual([
      'prompt_tokens', 'completion_tokens', 'total_tokens',
      'cached_tokens', 'cache_write_tokens', 'cache_writes',
    ]);
    // a boolean is not a count; the aggregate is summed from the breakdown; a
    // zero entry is dropped; the ttl is lower-cased
    expect(e.usage).toEqual({
      prompt_tokens: 10, completion_tokens: 2, total_tokens: 12,
      cached_tokens: null, cache_write_tokens: 5, cache_writes: [{ tokens: 5, ttl: '1h' }],
    });
    expect(e.cache_ttl_policy).toBe('1h');
    expect(event('2026-10-11T12:00:00.000Z', 'p', { cache_ttl_policy: '2h' }).cache_ttl_policy).toBeNull();
    expect(event('2026-10-11T12:00:00.000Z', 'p', { cache_ttl_policy: ' EXPLICIT ' }).cache_ttl_policy).toBe('explicit');
    expect(event('2026-10-11T12:00:00.000Z', 'p', { usage: { cache_writes: 'nope' } }).usage).toBeNull();
    expect(CACHE_TTLS).toEqual(['5m', '1h', '30m', '24h', 'explicit']);
    expect(normalizeUsage({ prompt_tokens: 1, cached_tokens: 0, cache_write_tokens: -1, cache_writes: [] })).toEqual({
      prompt_tokens: 1, completion_tokens: null, total_tokens: null,
      cached_tokens: 0, cache_write_tokens: 0, cache_writes: [],
    });
    expect(normalizeCacheUsage('OpenAI', { prompt_tokens_details: { cached_tokens: 1024 }, cache_write_tokens: 2048 })).toEqual({
      cached_tokens: 1024, cache_write_tokens: 2048, cache_writes: null,
    });
    const empty = { cached_tokens: null, cache_write_tokens: null, cache_writes: null };
    expect(normalizeCacheUsage('anthropic', null)).toEqual(empty);
    expect(normalizeCacheUsage(null, { cached_tokens: 1 })).toEqual(empty);
  });

  it('reads the taxonomy version from benchmarks.json, then the manifest', () => {
    expect(taxonomyVersion(starterBundle())).toBeNull();
    expect(taxonomyVersion(null)).toBeNull();
    expect(taxonomyVersion({ benchmarks: { taxonomy_version: 't-b' }, manifest: { taxonomy_version: 't-m' } })).toBe('t-b');
    expect(taxonomyVersion({ benchmarks: {}, manifest: { taxonomy_version: ' t-m ' } })).toBe('t-m');
    expect(taxonomyVersion({ benchmarks: { taxonomy_version: '' }, manifest: {} })).toBeNull();
  });

  it('setProcessMode: managed needs a model and orders keys', () => {
    expect(() => setProcessMode(null, 'p', 'managed')).toThrow('managed mode needs --model <id>');
    expect(() => setProcessMode(null, 'p', 'fast')).toThrow('mode must be off, stable, dynamic or managed');
    const doc = setProcessMode({ processes: { p: { providers: ['x'], note: 1 } } }, 'p', 'managed', ' openai/gpt-5-mini ');
    expect(Object.keys(doc.processes.p)).toEqual(['mode', 'model', 'providers', 'note']);
    expect(doc.processes.p.model).toBe('openai/gpt-5-mini');
    const back = setProcessMode(doc, 'p', 'off', 'ignored');
    expect(back.processes.p).toEqual({ mode: 'off', providers: ['x'], note: 1 });
    expect(Object.keys(back.processes.p)).toEqual(['mode', 'providers', 'note']);
  });

  it('resolveMode carries the managed model of its level', () => {
    const remote = { default_mode: 'managed', processes: { a: { mode: 'managed', model: 'openai/gpt-5-mini' } } };
    expect(resolveMode('a', { remote })).toEqual({ mode: 'managed', source: 'remote', model: 'openai/gpt-5-mini' });
    expect(resolveMode('b', { remote })).toEqual({ mode: 'managed', source: 'remote', model: null });
    expect(resolveMode('a', { env: { TRYAII_MONITOR_MODE: 'managed' }, remote })).toEqual({
      mode: 'managed',
      source: 'env',
      model: null,
    });
  });

  it('policyAllowed fails closed without origins', () => {
    expect(policyAllowed(null, null, ['a'])).toBeNull();
    expect(policyAllowed(new ModelPolicy(), null, ['a'])).toBeNull();
    expect(policyAllowed(new ModelPolicy({ origin: 'no-prc-models' }), null, ['a'])).toEqual([]);
  });

  it('route and shadow candidate lists', () => {
    const ids = ['openai/a', 'anthropic/b', 'openai/c'];
    expect(routeAllowed('openrouter', ids, null, null)).toBeNull();
    expect(routeAllowed('openrouter', ids, null, ['openai/c', 'anthropic/b'])).toEqual(['anthropic/b', 'openai/c']);
    expect(routeAllowed('openai', ids, 'openai/a', ['anthropic/b', 'openai/c'])).toEqual(['openai/c']);
    expect(routeAllowed('other', ids, 'openai/a', null)).toEqual(['openai/a']);
    expect(shadowAllowed(ids, null)).toBeNull();
    expect(shadowAllowed(ids, new Set(['openai/c', 'openai/a']))).toEqual(['openai/a', 'openai/c']);
  });

  it('pickWinner never recommends a disallowed model', () => {
    const registry = ModelRegistry.fromBundle(starterBundle());
    const ranked = [
      { modelId: 'openai/gpt-5-mini', qualityScore: 0.8 },
      { modelId: 'openai/gpt-5.5', qualityScore: 0.82 },
    ];
    expect(pickWinner(ranked, 'openai/gpt-5.5', registry).winner).toBe('openai/gpt-5-mini');
    expect(pickWinner(ranked, 'openai/gpt-5.5', registry, ['openai/gpt-5.5']).winner).toBe('openai/gpt-5.5');
    expect(pickWinner(ranked, 'openai/gpt-5.5', registry, [])).toEqual({ winner: null, rank: null, method: 'none' });
  });

  it('round-trips remote.json and upload.json', () => {
    expect(store.readRemote(dir)).toBeNull();
    writeFileSync(join(dir, 'remote.json'), '{"schema": "other"}', 'utf-8');
    expect(store.readRemote(dir)).toBeNull();
    store.writeRemote(dir, { schema: 'tryaii.monitor.remote/1', etag: 'e1', fetched_at: 'old' }, '2026-10-12T00:00:00.000Z');
    const doc = store.readRemote(dir) as Record<string, unknown>;
    expect(Object.keys(doc)).toEqual(['schema', 'etag', 'fetched_at']);
    expect(doc.fetched_at).toBe('2026-10-12T00:00:00.000Z');

    expect(store.readUploadCursor(dir)).toBeNull();
    store.writeUploadCursor(dir, { files: { 'events-b.jsonl': 3, 'events-a.jsonl': 5 }, last_ok: null, last_error: 'network' });
    const text = readFileSync(join(dir, 'upload.json'), 'utf-8');
    expect(text.startsWith('{\n  "schema": "tryaii.monitor.upload-cursor/1",')).toBe(true);
    expect(text.endsWith('}\n')).toBe(true);
    const cursor = store.readUploadCursor(dir);
    expect(Object.keys(cursor?.files ?? {})).toEqual(['events-a.jsonl', 'events-b.jsonl']);
    expect(cursor?.last_error).toBe('network');
  });

  it('counts the uploaded lines inside the cursor prefix', () => {
    writeFileSync(join(dir, 'events-2026-10-12.jsonl'), '{"a":1}\n{"b":2}\n{"c":3}\n');
    const cursor = {
      schema: store.UPLOAD_CURSOR_SCHEMA,
      files: { 'events-2026-10-12.jsonl': 16, 'events-gone.jsonl': 9 },
      last_ok: null,
      last_error: null,
    };
    expect(store.uploadedEventCount(dir, cursor)).toBe(2);
    expect(store.uploadedEventCount(dir, null)).toBe(0);
  });

  it('status source and upload blocks', () => {
    expect(sourceInfo(null)).toEqual({ kind: 'local', etag: null, fetched_at: null });
    expect(sourceInfo({ etag: 'e', fetched_at: 't' })).toEqual({ kind: 'remote', etag: 'e', fetched_at: 't' });
    expect(uploadInfo(null)).toEqual({ state: 'off', events: 0, last_ok: null, last_error: null });
    expect(uploadInfo({ last_ok: 't', last_error: null }, 7).state).toBe('ok');
    expect(uploadInfo({ last_ok: 't', last_error: 'http 503' }, 7).state).toBe('error');
  });
});
