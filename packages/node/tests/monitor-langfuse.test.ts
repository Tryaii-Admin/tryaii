/**
 * The Langfuse export (shared/monitor/SPEC.md "Langfuse export", TS-245/246).
 *
 * 1. The frozen `langfuse` fixture suite, generated from the PYTHON reference
 *    (scripts/gen-langfuse-fixtures.py): every expected block reproduced, key
 *    order included, and every request body byte for byte (`bodies_sha256`).
 * 2. The exporter against a fake POST: cursor, scores after the span,
 *    retries without duplicates, key rejection, fail-open.
 * 3. The `otel` capture in the wrapper (a fake OpenTelemetry global).
 * No test touches the network or a real Langfuse.
 */

import { createHash } from 'node:crypto';
import { appendFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import * as lf from '../src/monitor/langfuse.js';
import { activeSpanIds } from '../src/monitor/otel.js';
import { store, wrap } from '../src/monitor/index.js';

const CASES = join(
  dirname(fileURLToPath(import.meta.url)),
  '..',
  '..',
  '..',
  'shared',
  'monitor',
  'fixtures',
  'langfuse',
  'cases.json',
);
const APP_KEY = 'tryaii_k1a2b3c4d5e6_' + 'fixtureSecret'.repeat(4).slice(0, 43);

type Rec = Record<string, any>;

const sha = (text: string): string => createHash('sha256').update(text, 'utf-8').digest('hex');

// --- 1. fixtures --------------------------------------------------------------

function genCase(input: Rec, version: string): unknown {
  if (input.kind === 'export') {
    const events = input.events as Rec[];
    const spans = events.map((e) => lf.buildSpan(e, input.app_id ?? null));
    const body = lf.buildTracesBody(spans, version);
    const scores = events.map((e) => lf.buildScores(e));
    return {
      ids: events.map((e) => lf.spanIds(e)),
      tryaii_url: events.map((e) => lf.tryaiiUrl(input.app_id ?? null, e.process)),
      traces: body,
      scores,
      bodies_sha256: {
        traces: sha(JSON.stringify(body)),
        scores: scores.map((perEvent) => perEvent.map((s) => sha(JSON.stringify(s)))),
      },
    };
  }
  if (input.kind === 'settings') return { settings: lf.settings(input.env), status_line: lf.statusLine(input.env) };
  if (input.kind === 'exportable') return lf.exportable(input.line);
  throw new Error(`unknown case kind: ${String(input.kind)}`);
}

const doc = existsSync(CASES) ? (JSON.parse(readFileSync(CASES, 'utf-8')) as Rec) : { version: '', cases: [] };

describe.skipIf(!existsSync(CASES))('langfuse fixtures', () => {
  for (const c of doc.cases as Rec[]) {
    it(c.name, () => {
      const actual = genCase(c.input, doc.version);
      expect(actual).toEqual(c.expected);
      expect(JSON.stringify(actual)).toBe(JSON.stringify(c.expected));
    });
  }
});

// --- 2. exporter --------------------------------------------------------------

interface Req {
  path: string;
  auth: string;
  body: Rec;
  raw: string;
}

class FakePost {
  requests: Req[] = [];
  constructor(private readonly answers: Record<string, (number | 'network')[]> = {}) {}

  readonly post: lf.PostFn = async (url, body, auth) => {
    const path = new URL(url).pathname;
    this.requests.push({ path, auth, body: JSON.parse(body.toString('utf-8')), raw: body.toString('utf-8') });
    const queue = this.answers[path] ?? [200];
    const answer = queue.length > 1 ? (queue.shift() as number | 'network') : queue[0];
    if (answer === 'network') throw new Error('connection refused');
    return answer;
  };

  paths(): string[] {
    return this.requests.map((r) => r.path);
  }
}

function event(n = 1, extra: Rec = {}): Rec {
  return {
    schema: 'tryaii.monitor.event/1',
    event_id: `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`,
    ts: '2026-10-24T09:15:30.250Z',
    app: 'shop',
    process: 'checkout',
    call_site: { file: '/home/dev/shop/checkout.ts', line: 3, function: 'buy' },
    provider: 'openai',
    model_requested: 'gpt-5.5',
    model_resolved: 'openai/gpt-5.5',
    model_used: 'gpt-5.5',
    mode: 'off',
    winner: 'openai/gpt-5-mini',
    winner_reachable: true,
    route_status: 'ok',
    classification: null,
    taxonomy_version: null,
    usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 },
    cache_ttl_policy: null,
    est_cost_usd: 0.0002,
    est_cost_winner_usd: 0.00001,
    est_cost_anchor_usd: 0.0002,
    latency_ms: 50,
    stream: false,
    error: null,
    prompt_hash: 'ab'.repeat(32),
    vector: false,
    ...extra,
  };
}

const FILE = 'events-2026-10-24.jsonl';

function append(directory: string, events: Rec[], raw = ''): string {
  mkdirSync(directory, { recursive: true });
  const path = join(directory, FILE);
  appendFileSync(path, events.map((e) => JSON.stringify(e) + '\n').join('') + raw);
  return path;
}

let tmp: string;
let now = 1000;

function exporter(fake: FakePost): lf.Exporter {
  return new lf.Exporter('http://lf.test/', 'pk-lf-1', 'sk-lf-1', {
    post: fake.post,
    clock: () => now,
    nowTs: () => '2026-10-24T10:00:00.000Z',
    version: 'test',
  });
}

function spansOf(req: Req): Rec[] {
  return req.body.resourceSpans[0].scopeSpans[0].spans;
}

describe('langfuse exporter', () => {
  beforeEach(() => {
    tmp = mkdtempSync(join(tmpdir(), 'tryaii-langfuse-'));
    now = 1000;
  });
  afterEach(() => rmSync(tmp, { recursive: true, force: true }));

  it('starts at the end of the store (no backfill)', async () => {
    const d = join(tmp, 'shop');
    append(d, [event(1)]);
    const fake = new FakePost();
    const ex = exporter(fake);
    ex.register(d);
    expect(lf.readCursor(d)).toEqual({
      schema: lf.CURSOR_SCHEMA,
      host: 'http://lf.test',
      files: { [FILE]: statSync(join(d, FILE)).size },
      last_ok: null,
      last_error: null,
    });
    expect(await ex.runOnce()).toBe('idle');
    expect(fake.requests).toEqual([]);
  });

  it('sends the spans, then the scores, and advances', async () => {
    const d = join(tmp, 'shop');
    const fake = new FakePost();
    const ex = exporter(fake);
    ex.register(d);
    append(d, [event(1), event(2, { winner: null, est_cost_winner_usd: null })]);
    expect(await ex.runOnce()).toBe('sent');
    expect(fake.paths()).toEqual([lf.TRACES_PATH, ...Array(4).fill(lf.SCORES_PATH)]);
    const auth = Buffer.from('pk-lf-1:sk-lf-1').toString('base64');
    expect(new Set(fake.requests.map((r) => r.auth))).toEqual(new Set([auth]));
    expect(spansOf(fake.requests[0]).map((s) => s.spanId)).toEqual([1, 2].map((n) => lf.derivedIds(event(n).event_id)[1]));
    expect(fake.requests.slice(1).map((r) => r.body.name).sort()).toEqual(
      [lf.SCORE_RECOMMENDED, lf.SCORE_SAVINGS, lf.SCORE_ROUTE_STATUS, lf.SCORE_ROUTE_STATUS].sort(),
    );
    const raw = fake.requests.map((r) => r.raw).join('');
    expect(raw).not.toContain('prompt_hash');
    expect(raw).not.toContain('abab');
    expect(raw).not.toContain('/home/dev');
    const cursor = lf.readCursor(d)!;
    expect(cursor.files[FILE]).toBe(statSync(join(d, FILE)).size);
    expect(cursor.last_ok).toBe('2026-10-24T10:00:00.000Z');
    expect(cursor.last_error).toBeNull();
    expect([ex.sentSpans, ex.sentScores]).toEqual([2, 4]);
  });

  it('retries a failed score with the same span and score ids', async () => {
    const d = join(tmp, 'shop');
    const fake = new FakePost({ [lf.SCORES_PATH]: [200, 503, 200] });
    const ex = exporter(fake);
    ex.register(d);
    append(d, [event(1)]);
    expect(await ex.runOnce()).toBe('backoff');
    expect(lf.readCursor(d)!.files[FILE] ?? 0).toBe(0);
    expect(lf.readCursor(d)!.last_error).toBe('scores: http 503');
    expect(await ex.runOnce()).toBe('wait');
    now += lf.BACKOFF_MIN_MS;
    expect(await ex.runOnce()).toBe('sent');
    const [first, second] = [fake.requests.slice(0, 4), fake.requests.slice(4)];
    expect(second.map((r) => r.path)).toEqual([lf.TRACES_PATH, lf.SCORES_PATH, lf.SCORES_PATH, lf.SCORES_PATH]);
    expect(second[0].raw).toBe(first[0].raw);
    expect(second.slice(1).map((r) => r.body.id).sort()).toEqual(lf.buildScores(event(1)).map((s) => s.id).sort());
    expect(lf.readCursor(d)!.last_error).toBeNull();
  });

  it('backs off on a network failure and never throws', async () => {
    const d = join(tmp, 'shop');
    const ex = exporter(new FakePost({ [lf.TRACES_PATH]: ['network'] }));
    ex.register(d);
    append(d, [event(1)]);
    expect(await ex.runOnce()).toBe('backoff');
    expect(ex.backoff).toBe(lf.BACKOFF_MIN_MS);
    expect(lf.readCursor(d)!.last_error).toBe('traces: network');
  });

  it('turns itself off when Langfuse rejects the keys', async () => {
    const d = join(tmp, 'shop');
    const fake = new FakePost({ [lf.TRACES_PATH]: [401] });
    const lines: string[] = [];
    const original = lf.warnings.write;
    lf.warnings.write = (text) => void lines.push(text);
    try {
      const ex = exporter(fake);
      ex.register(d);
      append(d, [event(1)]);
      expect(await ex.runOnce()).toBe('disabled');
      expect(await ex.runOnce()).toBe('disabled');
    } finally {
      lf.warnings.write = original;
    }
    expect(fake.requests.length).toBe(1);
    expect(lines.join('')).toContain('Langfuse rejected the keys');
    expect(lf.readCursor(d)!.last_error).toMatch(/^traces: http 401/);
  });

  it('passes over a batch Langfuse refuses (400), without scores', async () => {
    const d = join(tmp, 'shop');
    const fake = new FakePost({ [lf.TRACES_PATH]: [400] });
    const ex = exporter(fake);
    ex.register(d);
    append(d, [event(1)]);
    expect(await ex.runOnce()).toBe('sent');
    expect(lf.readCursor(d)!.last_error).toBe('traces: http 400');
    expect(fake.paths()).toEqual([lf.TRACES_PATH]);
  });

  it('waits for a partial line and skips unexportable ones', async () => {
    const d = join(tmp, 'shop');
    const fake = new FakePost();
    const ex = exporter(fake);
    ex.register(d);
    const partial = '{"schema":"tryaii.monitor.event/1"';
    const path = append(d, [event(1), event(2, { messages: [{ role: 'user' }] })], partial);
    expect(await ex.runOnce()).toBe('sent');
    expect(spansOf(fake.requests[0]).length).toBe(1);
    expect(lf.readCursor(d)!.files[FILE]).toBe(statSync(path).size - partial.length);
  });

  it('links tryaii_url to the app id from remote.json', async () => {
    const d = join(tmp, 'shop');
    const fake = new FakePost();
    const ex = exporter(fake);
    ex.register(d);
    store.writeRemote(
      d,
      { schema: 'tryaii.monitor.remote/1', etag: 'e', app_id: 'app_abc123def456', default_mode: null, processes: {} },
      '2026-10-24T09:00:00.000Z',
    );
    append(d, [event(1)]);
    await ex.runOnce();
    const attrs = spansOf(fake.requests[0])[0].attributes as Rec[];
    const url = attrs.find((a) => a.key === 'langfuse.observation.metadata.tryaii_url')!.value.stringValue;
    expect(url).toBe('https://tryaii.com/dashboard/monitor/app_abc123def456/features/checkout');
  });

  it('fromEnv warns once when the switch is on but a key is missing', () => {
    lf.resetWarning();
    const lines: string[] = [];
    const original = lf.warnings.write;
    lf.warnings.write = (text) => void lines.push(text);
    try {
      expect(lf.Exporter.fromEnv({ TRYAII_LANGFUSE: 'on' })).toBeNull();
      expect(lf.Exporter.fromEnv({ TRYAII_LANGFUSE: 'on' })).toBeNull();
      expect(lf.Exporter.fromEnv({})).toBeNull();
    } finally {
      lf.warnings.write = original;
    }
    expect(lines).toEqual(['tryaii monitor: the Langfuse export is off (TRYAII_API_KEY is not set)\n']);
    const ex = lf.Exporter.fromEnv({
      TRYAII_LANGFUSE: 'on',
      TRYAII_API_KEY: APP_KEY,
      LANGFUSE_PUBLIC_KEY: 'pk',
      LANGFUSE_SECRET_KEY: 'sk',
      LANGFUSE_HOST: 'http://localhost:3100/',
    });
    expect(ex?.host).toBe('http://localhost:3100');
  });

  it('restarts the cursor when the host changes', async () => {
    const d = join(tmp, 'shop');
    append(d, [event(1)]);
    lf.writeCursor(d, { schema: lf.CURSOR_SCHEMA, host: 'https://cloud.langfuse.com', files: { [FILE]: 0 }, last_ok: null, last_error: null });
    const ex = exporter(new FakePost());
    ex.register(d);
    expect(lf.readCursor(d)!.host).toBe('http://lf.test');
    expect(await ex.runOnce()).toBe('idle');
  });

  it('writes the cursor in the Python byte format', () => {
    const d = join(tmp, 'shop');
    mkdirSync(d, { recursive: true });
    lf.writeCursor(d, { schema: lf.CURSOR_SCHEMA, host: 'h', files: { b: 2, a: 1 }, last_ok: null, last_error: 'x' });
    expect(readFileSync(join(d, lf.CURSOR_FILE), 'utf-8')).toBe(
      '{\n  "schema": "tryaii.monitor.langfuse-cursor/1",\n  "host": "h",\n  "files": {\n    "a": 1,\n    "b": 2\n  },\n  "last_ok": null,\n  "last_error": "x"\n}\n',
    );
  });
});

// --- 3. otel capture ------------------------------------------------------------

const GLOBAL_KEY = Symbol.for('opentelemetry.js.api.1');
const SPAN_KEY = Symbol.for('OpenTelemetry Context Key SPAN');

function fakeOtel(traceId: string | null, spanId = '00f067aa0ba902b7'): void {
  const g = globalThis as Record<symbol, unknown>;
  if (traceId === null) {
    delete g[GLOBAL_KEY];
    return;
  }
  const span = { spanContext: () => ({ traceId, spanId, traceFlags: 1 }) };
  g[GLOBAL_KEY] = { version: '1.9.0', context: { active: () => ({ getValue: (k: symbol) => (k === SPAN_KEY ? span : undefined) }) } };
}

describe('otel capture', () => {
  let dataDir: string;
  beforeEach(() => {
    dataDir = mkdtempSync(join(tmpdir(), 'tryaii-langfuse-wrap-'));
    process.env.TRYAII_DRE_DATA_DIR = dataDir;
    delete process.env.TRYAII_MONITOR_MODE;
    delete process.env.TRYAII_APP;
  });
  afterEach(() => {
    fakeOtel(null);
    delete process.env.TRYAII_DRE_DATA_DIR;
    rmSync(dataDir, { recursive: true, force: true });
  });

  it('reads the active span from the OpenTelemetry global', () => {
    fakeOtel('4bf92f3577b34da6a3ce929d0e0e4736');
    expect(activeSpanIds()).toEqual({ trace_id: '4bf92f3577b34da6a3ce929d0e0e4736', span_id: '00f067aa0ba902b7' });
    fakeOtel('00000000000000000000000000000000');
    expect(activeSpanIds()).toBeNull();
    fakeOtel(null);
    expect(activeSpanIds()).toBeNull();
    (globalThis as Record<symbol, unknown>)[GLOBAL_KEY] = { context: { active: () => { throw new Error('x'); } } };
    expect(activeSpanIds()).toBeNull();
  });

  for (const active of [true, false]) {
    it(`records otel on the event (${active ? 'active span' : 'no span'})`, async () => {
      fakeOtel(active ? '4bf92f3577b34da6a3ce929d0e0e4736' : null);
      const client = {
        baseURL: 'https://api.openai.com/v1',
        chat: {
          completions: {
            create: (body: Rec) =>
              Promise.resolve({ model: body.model, usage: { prompt_tokens: 3, completion_tokens: 2, total_tokens: 5 } }),
          },
        },
      };
      const monitor = wrap(client, {
        app: 'shop',
        process: 'p',
        routeFn: () => [[{ modelId: 'openai/gpt-5-mini', qualityScore: 0.8 }], null, null],
      });
      await (client.chat.completions.create as (b: Rec) => Promise<unknown>)({
        model: 'gpt-5.5',
        messages: [{ role: 'user', content: 'hi' }],
      });
      await monitor.drain();
      const events = store.readRecentEvents(monitor.dir, 5);
      const ev = events[events.length - 1] as Rec;
      if (active) {
        expect(ev.otel).toEqual({ trace_id: '4bf92f3577b34da6a3ce929d0e0e4736', span_id: '00f067aa0ba902b7' });
        const keys = Object.keys(ev);
        expect(keys.indexOf('otel')).toBe(keys.indexOf('vector') + 1);
      } else {
        expect('otel' in ev).toBe(false);
      }
    });
  }
});

// --- 4. Node: the generation observeOpenAI starts for the call -----------------

describe("Langfuse's observeOpenAI generation", () => {
  let dataDir: string;
  const ENV = {
    TRYAII_LANGFUSE: 'on',
    TRYAII_API_KEY: APP_KEY,
    LANGFUSE_PUBLIC_KEY: 'pk',
    LANGFUSE_SECRET_KEY: 'sk',
  };
  let counter = 0;
  const spanIdsSeq = (): { traceId: string; spanId: string } => {
    counter += 1;
    return { traceId: 'a'.repeat(31) + String(counter % 10), spanId: 'b'.repeat(15) + String(counter % 10) };
  };
  let provider: Rec;

  beforeEach(() => {
    dataDir = mkdtempSync(join(tmpdir(), 'tryaii-langfuse-gen-'));
    process.env.TRYAII_DRE_DATA_DIR = dataDir;
    delete process.env.TRYAII_MONITOR_MODE;
    delete process.env.TRYAII_APP;
    Object.assign(process.env, ENV);
    const tracer = { startSpan: () => { const sc = spanIdsSeq(); return { spanContext: () => sc }; } };
    provider = { getTracer: () => tracer };
    (globalThis as Record<symbol, unknown>)[GLOBAL_KEY] = { version: '1.9.0', trace: provider };
  });
  afterEach(() => {
    delete (globalThis as Record<symbol, unknown>)[GLOBAL_KEY];
    for (const k of Object.keys(ENV)) delete process.env[k];
    delete process.env.TRYAII_DRE_DATA_DIR;
    rmSync(dataDir, { recursive: true, force: true });
  });

  function client(): Rec {
    return {
      baseURL: 'https://api.openai.com/v1',
      chat: {
        completions: {
          create: (body: Rec) =>
            Promise.resolve({ model: body.model, usage: { prompt_tokens: 3, completion_tokens: 2, total_tokens: 5 } }),
        },
      },
    };
  }

  const route = (): [unknown[], null, null] => [[{ modelId: 'openai/gpt-5-mini', qualityScore: 0.8 }], null, null];
  const body = { model: 'gpt-5.5', messages: [{ role: 'user', content: 'hi' }] };

  it('takes the generation started right before the call (the TryAii span is its child)', async () => {
    const c = client();
    const monitor = wrap(c, { app: 'shop', process: 'p', routeFn: route });
    // What observeOpenAI's wrapMethod does: startObservation, then the patched create, same tick.
    const tracer = (provider.getTracer as (n: string) => Rec)('langfuse-sdk');
    const gen = (tracer.startSpan as () => Rec)();
    await c.chat.completions.create(body);
    await monitor.drain();
    const ev = store.readRecentEvents(monitor.dir, 5).pop() as Rec;
    const sc = (gen.spanContext as () => Rec)();
    expect(ev.otel).toEqual({ trace_id: sc.traceId, span_id: sc.spanId });
    expect(lf.spanIds(ev).parent_span_id).toBe(sc.spanId);
    expect(lf.buildSpan(ev).attributes[0].value.stringValue).toBe('span');
  });

  it('never takes a span from an earlier tick, or from another tracer', async () => {
    const c = client();
    const monitor = wrap(c, { app: 'shop', process: 'p', routeFn: route });
    const tracer = (provider.getTracer as (n: string) => Rec)('langfuse-sdk');
    (tracer.startSpan as () => Rec)();
    await Promise.resolve();
    await c.chat.completions.create(body);
    (provider.getTracer as (n: string) => Rec)('other-lib');
    await monitor.drain();
    expect('otel' in (store.readRecentEvents(monitor.dir, 5).pop() as Rec)).toBe(false);
  });

  it('watches nothing while the export is off', async () => {
    delete process.env.TRYAII_LANGFUSE;
    const original = provider.getTracer;
    const c = client();
    const monitor = wrap(c, { app: 'shop', process: 'p', routeFn: route });
    expect(provider.getTracer).toBe(original);
    await c.chat.completions.create(body);
    await monitor.drain();
    expect('otel' in (store.readRecentEvents(monitor.dir, 5).pop() as Rec)).toBe(false);
  });
});
