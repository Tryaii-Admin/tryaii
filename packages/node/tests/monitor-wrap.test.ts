/**
 * The openai wrapper, transport and vectors (shared/monitor/SPEC.md §2-§3).
 *
 * Everything runs against a duck-typed fake client and an injected routeFn
 * (no openai import, no daemon, no embedding model). The clock is pinned
 * through `clock.now`. Port of packages/python/tests/test_monitor_wrap.py.
 */

import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  Monitor,
  Transport,
  clock,
  defaults,
  install,
  process as inProcess,
  providerClass,
  store,
  wrap,
} from '../src/monitor/index.js';
import { makeCreate } from '../src/monitor/wrapOpenai.js';
import { ModelPolicy, savePolicy } from '../src/policy.js';

const NOW = Date.UTC(2026, 9, 11, 12, 0, 0, 123);
const MESSAGES = [
  { role: 'system', content: 'You are terse.' },
  { role: 'user', content: 'Fix the failing test in checkout.py' },
];
const CLASSIFICATION = { broadCategory: 'coding', subcategory: 'debugging', confidence: 0.8123 };
const EMBEDDING = { model: 'fake-embed', dim: 3, v: [0.1234, -0.5, 1] };

// Starter-catalog prices (USD / 1K, blended): gpt-5.5 0.0175, gpt-5-mini 0.001125,
// claude-haiku-4.5 0.003. quality floor = anchor - 0.05.
const SCORES_MINI_WINS = [
  { modelId: 'openai/gpt-5-mini', qualityScore: 0.8 },
  { modelId: 'openai/gpt-5.5', qualityScore: 0.82 },
  { modelId: 'anthropic/claude-haiku-4.5', qualityScore: 0.79 },
];
const SCORES_HAIKU_WINS = [
  { modelId: 'anthropic/claude-haiku-4.5', qualityScore: 0.81 },
  { modelId: 'openai/gpt-5.5', qualityScore: 0.82 },
  { modelId: 'openai/gpt-5-mini', qualityScore: 0.7 },
];

// --- fakes ------------------------------------------------------------------

function usage(prompt = 100, completion = 50) {
  return { prompt_tokens: prompt, completion_tokens: completion, total_tokens: prompt + completion };
}

class RateLimitError extends Error {
  status = 429;
}

interface Chunk {
  text: string;
  usage?: ReturnType<typeof usage> | null;
}

class FakeStream implements AsyncIterable<Chunk> {
  controller = { aborted: false };
  closed = false;
  constructor(private readonly chunks: Chunk[]) {}
  async *[Symbol.asyncIterator](): AsyncIterator<Chunk> {
    try {
      for (const c of this.chunks) yield c;
    } finally {
      this.closed = true;
    }
  }
}

class FakeCompletions {
  calls: Record<string, unknown>[] = [];
  constructor(
    private readonly opts: { raise?: Error | null; usage?: boolean; sync?: boolean } = {},
  ) {}

  create(body: Record<string, unknown>): unknown {
    this.calls.push({ ...body });
    if (this.opts.raise) {
      if (this.opts.sync) throw this.opts.raise;
      return Promise.reject(this.opts.raise);
    }
    const withUsage = this.opts.usage !== false;
    if (body.stream) {
      const last: Chunk = { text: '', usage: withUsage ? usage() : null };
      return Promise.resolve(new FakeStream([{ text: 'a' }, { text: 'b' }, last]));
    }
    return Promise.resolve({ model: body.model, usage: withUsage ? usage() : null });
  }
}

class FakeClient {
  chat: { completions: FakeCompletions };
  constructor(
    public baseURL = 'https://api.openai.com/v1',
    completions?: FakeCompletions,
  ) {
    this.chat = { completions: completions ?? new FakeCompletions() };
  }
}

type RouteFnResult = [unknown[], unknown, unknown];

function makeRouteFn(scores: unknown[], embedding: unknown = EMBEDDING, calls?: string[]) {
  return (text: string): RouteFnResult => {
    calls?.push(text);
    return [scores, CLASSIFICATION, embedding];
  };
}

function eventsOf(monitor: Monitor): Record<string, unknown>[] {
  return store.readRecentEvents(monitor.dir, 50);
}

function vectorsOf(monitor: Monitor): Record<string, unknown>[] {
  return store.readLines(store.vectorsPath(monitor.dir, new Date(NOW).toISOString()));
}

async function call(client: FakeClient, body: Record<string, unknown>): Promise<unknown> {
  const result = await client.chat.completions.create(body);
  return result;
}

let dataDir: string;
const savedNow = clock.now;
const savedEnv: Record<string, string | undefined> = {};

beforeEach(() => {
  dataDir = mkdtempSync(join(tmpdir(), 'tryaii-monitor-wrap-'));
  for (const key of ['TRYAII_DRE_DATA_DIR', 'TRYAII_MONITOR_MODE', 'TRYAII_APP', 'TRYAII_MONITOR_VECTORS']) {
    savedEnv[key] = process.env[key];
    delete process.env[key];
  }
  process.env.TRYAII_DRE_DATA_DIR = dataDir;
  clock.now = () => NOW;
});

afterEach(() => {
  clock.now = savedNow;
  for (const [key, value] of Object.entries(savedEnv)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
  rmSync(dataDir, { recursive: true, force: true });
});

// --- off: capture + shadow routing -------------------------------------------

describe('off', () => {
  it('records an event and a vector without touching the call', async () => {
    const client = new FakeClient();
    const monitor = wrap(client, { app: 'shop', process: 'checkout', routeFn: makeRouteFn(SCORES_MINI_WINS) });
    const resp = (await call(client, { model: 'gpt-5.5', messages: MESSAGES })) as { model: string };
    await monitor.drain();

    expect(resp.model).toBe('gpt-5.5');
    expect(client.chat.completions.calls[0].model).toBe('gpt-5.5');
    expect('tryaii_process' in client.chat.completions.calls[0]).toBe(false);
    const [event] = eventsOf(monitor);
    expect(Object.keys(event)).toEqual([
      'schema', 'event_id', 'ts', 'app', 'process', 'call_site', 'provider',
      'model_requested', 'model_resolved', 'model_used', 'mode', 'winner',
      'winner_reachable', 'route_status', 'classification', 'taxonomy_version', 'usage',
      'est_cost_usd', 'est_cost_winner_usd', 'est_cost_anchor_usd', 'latency_ms',
      'stream', 'error', 'prompt_hash', 'vector',
    ]);
    expect(event.event_id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    expect(event.taxonomy_version).toBeNull(); // no bundle carries one yet
    expect(event.ts).toBe('2026-10-11T12:00:00.123Z');
    expect(event.app).toBe('shop');
    expect(event.process).toBe('checkout');
    const site = event.call_site as Record<string, unknown>;
    expect(String(site.file).replace(/\\/g, '/')).toMatch(/monitor-wrap\.test\.ts$/);
    expect(typeof site.line).toBe('number');
    expect(event.provider).toBe('openai');
    expect(event.model_requested).toBe('gpt-5.5');
    expect(event.model_resolved).toBe('openai/gpt-5.5');
    expect(event.model_used).toBe('gpt-5.5');
    expect(event.mode).toBe('off');
    expect(event.winner).toBe('openai/gpt-5-mini');
    expect(event.winner_reachable).toBe(true);
    expect(event.route_status).toBe('ok');
    expect(event.classification).toEqual({ category: 'coding', subcategory: 'debugging', confidence: 0.8123 });
    expect(event.usage).toEqual({ prompt_tokens: 100, completion_tokens: 50, total_tokens: 150 });
    expect(event.est_cost_usd).toBe(0.002); // 100*0.005/1k + 50*0.03/1k
    expect(event.est_cost_winner_usd).toBe(0.000125);
    expect(event.est_cost_anchor_usd).toBe(0.002); // no swap: equals est_cost_usd
    expect(event.stream).toBe(false);
    expect(event.error).toBeNull();
    expect(event.vector).toBe(true);
    const [vector] = vectorsOf(monitor);
    expect(vector.prompt_hash).toBe(event.prompt_hash);
    expect(vector.model).toBe('fake-embed');
    expect(vector.dim).toBe(3);
    expect(vector.v).toEqual([0.1234, -0.5, 1]);
    expect(vector.process).toBe('checkout');
  });

  it('returns the provider promise untouched and never waits for routing on the hot path', async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    let routed = false;
    const slowRoute = async (): Promise<RouteFnResult> => {
      await gate;
      routed = true;
      return [SCORES_MINI_WINS, CLASSIFICATION, null];
    };
    const transport = new Transport(null, { routeFn: slowRoute, inline: false });
    transport.start();
    const client = new FakeClient();
    const monitor = new Monitor('openai', { app: 'shop', process: 'p', transport });
    const completions = client.chat.completions as unknown as { create: (body: Record<string, unknown>) => unknown };
    const original = completions.create.bind(client.chat.completions);
    let last: unknown = null;
    completions.create = makeCreate(monitor, (body) => {
      last = original(body);
      return last;
    });
    const p = client.chat.completions.create({ model: 'gpt-5.5', messages: MESSAGES });
    expect(p).toBe(last); // the provider's own promise comes back
    await p;
    expect(routed).toBe(false); // the call completed before routing ran
    expect(eventsOf(monitor)).toEqual([]);
    release();
    await transport.drain(5000);
    expect(routed).toBe(true);
    const [event] = eventsOf(monitor);
    expect(event.winner).toBe('openai/gpt-5-mini');
    expect(event.vector).toBe(false);
  });

  it('vectors can be disabled', async () => {
    process.env.TRYAII_MONITOR_VECTORS = '0';
    const client = new FakeClient();
    const monitor = wrap(client, { app: 'shop', routeFn: makeRouteFn(SCORES_MINI_WINS) });
    await call(client, { model: 'gpt-5.5', messages: MESSAGES });
    await monitor.drain();
    const [event] = eventsOf(monitor);
    expect(event.vector).toBe(false);
    expect(vectorsOf(monitor)).toEqual([]);
  });

  it('writes one vector for the same prompt twice', async () => {
    const client = new FakeClient();
    const monitor = wrap(client, { app: 'shop', routeFn: makeRouteFn(SCORES_MINI_WINS) });
    await call(client, { model: 'gpt-5.5', messages: MESSAGES });
    await monitor.drain();
    await call(client, { model: 'gpt-5.5', messages: MESSAGES });
    await monitor.drain();
    expect(eventsOf(monitor).map((e) => e.vector)).toEqual([true, false]);
    expect(vectorsOf(monitor)).toHaveLength(1);
  });

  it('records but does not route a call without user text', async () => {
    const calls: string[] = [];
    const client = new FakeClient();
    const monitor = wrap(client, { app: 'shop', routeFn: makeRouteFn(SCORES_MINI_WINS, EMBEDDING, calls) });
    await call(client, { model: 'gpt-5.5', messages: [{ role: 'system', content: 's' }] });
    await monitor.drain();
    const [event] = eventsOf(monitor);
    expect(event.route_status).toBe('skipped');
    expect(event.winner).toBeNull();
    expect(calls).toEqual([]);
  });

  it('records route failures as a status', async () => {
    const boom = (): RouteFnResult => {
      throw new Error('daemon gone');
    };
    const slow = (): RouteFnResult => {
      const err = new Error('slow');
      err.name = 'TimeoutError';
      throw err;
    };
    for (const [fn, status] of [[boom, 'unavailable'], [slow, 'timeout']] as const) {
      const client = new FakeClient();
      const monitor = wrap(client, { app: `app-${status}`, routeFn: fn });
      const resp = (await call(client, { model: 'gpt-5.5', messages: MESSAGES })) as { model: string };
      await monitor.drain();
      expect(resp.model).toBe('gpt-5.5');
      const [event] = eventsOf(monitor);
      expect(event.route_status).toBe(status);
      expect(event.winner).toBeNull();
      expect(event.est_cost_usd).toBe(0.002);
    }
  });
});

// --- dynamic ----------------------------------------------------------------

describe('dynamic', () => {
  it('sends the reachable winner', async () => {
    const calls: string[] = [];
    const client = new FakeClient();
    const monitor = wrap(client, { app: 'shop', mode: 'dynamic', routeFn: makeRouteFn(SCORES_MINI_WINS, EMBEDDING, calls) });
    await call(client, { model: 'gpt-5.5', messages: MESSAGES });
    await monitor.drain();
    expect(client.chat.completions.calls[0].model).toBe('gpt-5-mini');
    // In-path over the reachable (openai/*) candidates, then the shadow route
    // over the whole (policy-allowed) catalog for the recommendation.
    expect(calls).toHaveLength(2);
    const [event] = eventsOf(monitor);
    expect(event.mode).toBe('dynamic');
    expect(event.model_requested).toBe('gpt-5.5');
    expect(event.model_used).toBe('gpt-5-mini');
    expect(event.est_cost_usd).toBe(0.000125);
    expect(event.est_cost_winner_usd).toBe(0.000125);
    // After a swap est_cost_usd is priced at the model sent; the anchor price
    // makes the realised saving computable (API.md v1.1).
    expect(event.est_cost_anchor_usd).toBe(0.002);
  });

  it('keeps the anchor when the winner is unreachable', async () => {
    const client = new FakeClient();
    const monitor = wrap(client, { app: 'shop', mode: 'dynamic', routeFn: makeRouteFn(SCORES_HAIKU_WINS) });
    await call(client, { model: 'gpt-5.5', messages: MESSAGES });
    await monitor.drain();
    expect(client.chat.completions.calls[0].model).toBe('gpt-5.5');
    const [event] = eventsOf(monitor);
    expect(event.winner).toBe('anthropic/claude-haiku-4.5');
    expect(event.winner_reachable).toBe(false);
    expect(event.model_used).toBe('gpt-5.5');
  });

  it('reaches every provider through an openrouter client', async () => {
    const client = new FakeClient('https://openrouter.ai/api/v1');
    const monitor = wrap(client, { app: 'shop', mode: 'dynamic', routeFn: makeRouteFn(SCORES_HAIKU_WINS) });
    await call(client, { model: 'openai/gpt-5.5', messages: MESSAGES });
    await monitor.drain();
    expect(client.chat.completions.calls[0].model).toBe('anthropic/claude-haiku-4.5');
    const [event] = eventsOf(monitor);
    expect(event.provider).toBe('openrouter');
    expect(event.winner_reachable).toBe(true);
  });

  it('records but never swaps on another provider', async () => {
    const client = new FakeClient('http://localhost:11434/v1');
    const monitor = wrap(client, { app: 'shop', mode: 'dynamic', routeFn: makeRouteFn(SCORES_MINI_WINS) });
    await call(client, { model: 'gpt-5.5', messages: MESSAGES });
    await monitor.drain();
    expect(client.chat.completions.calls[0].model).toBe('gpt-5.5');
    const [event] = eventsOf(monitor);
    expect(event.provider).toBe('other');
    expect(event.winner).toBe('openai/gpt-5-mini');
    expect(event.winner_reachable).toBe(false);
  });

  it('takes the mode from config.json and the environment', async () => {
    const client = new FakeClient();
    const monitor = wrap(client, { app: 'shop', process: 'checkout', routeFn: makeRouteFn(SCORES_MINI_WINS) });
    store.writeConfig(monitor.dir, {
      schema: 'tryaii.monitor.config/1',
      source: 'local',
      default_mode: 'off',
      processes: { checkout: { mode: 'dynamic', providers: null } },
    });
    await call(client, { model: 'gpt-5.5', messages: MESSAGES });
    await monitor.drain();
    expect(client.chat.completions.calls.at(-1)?.model).toBe('gpt-5-mini');
    process.env.TRYAII_MONITOR_MODE = 'off';
    await call(client, { model: 'gpt-5.5', messages: MESSAGES });
    await monitor.drain();
    expect(client.chat.completions.calls.at(-1)?.model).toBe('gpt-5.5');
    expect(eventsOf(monitor).map((e) => e.mode)).toEqual(['dynamic', 'off']);
  });
});

// --- stable -----------------------------------------------------------------

describe('stable', () => {
  function pins(monitor: Monitor): Record<string, unknown> {
    return JSON.parse(readFileSync(join(monitor.dir, 'pins.json'), 'utf-8'));
  }

  it('pins the first winner and reuses it', async () => {
    const calls: string[] = [];
    const client = new FakeClient();
    const monitor = wrap(client, {
      app: 'shop', process: 'checkout', mode: 'stable', routeFn: makeRouteFn(SCORES_MINI_WINS, EMBEDDING, calls),
    });
    await call(client, { model: 'gpt-5.5', messages: MESSAGES });
    await monitor.drain();
    expect(client.chat.completions.calls[0].model).toBe('gpt-5-mini');
    const doc = pins(monitor);
    const pin = (doc.processes as Record<string, Record<string, unknown>>).checkout;
    expect(pin.model).toBe('openai/gpt-5-mini');
    expect(pin.pinned_at).toBe('2026-10-11T12:00:00.123Z');
    expect(pin.expires_at).toBe('2026-10-12T12:00:00.123Z');
    expect(pin.basis).toEqual({ window: 'first_call', events: 1, counts: { 'openai/gpt-5-mini': 1 } });
    expect(calls).toHaveLength(2); // in-path (reachable) + shadow (recommendation)

    // Second call: the pin is valid, so no in-path routing; shadow routing still runs.
    calls.length = 0;
    await call(client, { model: 'gpt-5.5', messages: MESSAGES });
    await monitor.drain();
    expect(client.chat.completions.calls[1].model).toBe('gpt-5-mini');
    expect(calls).toHaveLength(1);
    expect(pins(monitor)).toEqual(doc);
    expect(eventsOf(monitor).map((e) => e.mode)).toEqual(['stable', 'stable']);
  });

  it('first call pins the best reachable model, not the unreachable winner', async () => {
    const client = new FakeClient();
    const monitor = wrap(client, { app: 'shop', process: 'checkout', mode: 'stable', routeFn: makeRouteFn(SCORES_HAIKU_WINS) });
    await call(client, { model: 'gpt-5.5', messages: MESSAGES });
    await monitor.drain();
    // Over the reachable candidates nothing beats the anchor at its price: the
    // anchor is pinned (and sent as written); haiku stays the recommendation,
    // recorded as unreachable.
    expect(client.chat.completions.calls[0].model).toBe('gpt-5.5');
    const processes = pins(monitor).processes as Record<string, Record<string, unknown>>;
    expect(processes.checkout.model).toBe('openai/gpt-5.5');
    const [event] = eventsOf(monitor);
    expect(event.winner).toBe('anthropic/claude-haiku-4.5');
    expect(event.winner_reachable).toBe(false);
  });

  it('rebuilds an expired pin from history', async () => {
    const client = new FakeClient();
    const monitor = wrap(client, { app: 'shop', process: 'checkout', mode: 'stable', routeFn: makeRouteFn(SCORES_MINI_WINS) });
    for (let i = 0; i < 3; i++) {
      store.appendEvent(monitor.dir, {
        schema: 'tryaii.monitor.event/1', ts: `2026-10-11T0${i}:00:00.000Z`, app: 'shop', process: 'checkout',
        winner: 'openai/gpt-5-nano', winner_reachable: true, route_status: 'ok',
      });
    }
    store.writePins(monitor.dir, {
      schema: 'tryaii.monitor.pins/1',
      processes: {
        checkout: {
          model: 'openai/gpt-5-mini', pinned_at: '2026-10-09T00:00:00.000Z', expires_at: '2026-10-10T00:00:00.000Z',
          basis: { window: 'first_call', events: 1, counts: { 'openai/gpt-5-mini': 1 } },
        },
      },
    });
    await call(client, { model: 'gpt-5.5', messages: MESSAGES });
    await monitor.drain();
    expect(client.chat.completions.calls[0].model).toBe('gpt-5-nano');
    const pin = (pins(monitor).processes as Record<string, Record<string, unknown>>).checkout;
    expect(pin.model).toBe('openai/gpt-5-nano');
    expect((pin.basis as Record<string, unknown>).window).toBe('7d');
    expect((pin.basis as Record<string, unknown>).counts).toEqual({ 'openai/gpt-5-nano': 3 });
  });
});

// --- errors and streams -----------------------------------------------------

describe('errors and streams', () => {
  it('re-throws a provider error unchanged and records it', async () => {
    const exc = new RateLimitError('slow down');
    const client = new FakeClient(undefined, new FakeCompletions({ raise: exc }));
    const monitor = wrap(client, { app: 'shop', routeFn: makeRouteFn(SCORES_MINI_WINS) });
    await expect(call(client, { model: 'gpt-5.5', messages: MESSAGES })).rejects.toBe(exc);
    await monitor.drain();
    const [event] = eventsOf(monitor);
    expect(event.error).toEqual({ status: 429, class: 'RateLimitError' });
    expect(event.usage).toBeNull();
    expect(event.est_cost_usd).toBeNull();
    // A failed call is never routed and is written at once (survives an exit).
    expect(event.winner).toBeNull();
    expect(event.route_status).toBe('skipped');
  });

  it('re-throws a synchronous provider error too', async () => {
    const exc = new Error('bad request');
    const client = new FakeClient(undefined, new FakeCompletions({ raise: exc, sync: true }));
    const monitor = wrap(client, { app: 'shop', routeFn: makeRouteFn(SCORES_MINI_WINS) });
    expect(() => client.chat.completions.create({ model: 'gpt-5.5', messages: MESSAGES })).toThrow(exc);
    await monitor.drain();
    const [event] = eventsOf(monitor);
    expect(event.error).toEqual({ status: null, class: 'Error' });
  });

  it('forwards a stream and records it at the end', async () => {
    const client = new FakeClient();
    const monitor = wrap(client, { app: 'shop', routeFn: makeRouteFn(SCORES_MINI_WINS) });
    const stream = (await call(client, { model: 'gpt-5.5', messages: MESSAGES, stream: true })) as AsyncIterable<Chunk> & {
      controller: unknown;
    };
    expect(client.chat.completions.calls[0].stream_options).toEqual({ include_usage: true });
    expect(eventsOf(monitor)).toEqual([]);
    expect(stream.controller).toEqual({ aborted: false });
    const texts: string[] = [];
    for await (const c of stream) texts.push(c.text);
    expect(texts).toEqual(['a', 'b', '']);
    await monitor.drain();
    const [event] = eventsOf(monitor);
    expect(event.stream).toBe(true);
    expect(event.usage).toEqual({ prompt_tokens: 100, completion_tokens: 50, total_tokens: 150 });
  });

  it('records a stream left early', async () => {
    const client = new FakeClient();
    const monitor = wrap(client, { app: 'shop', routeFn: makeRouteFn(SCORES_MINI_WINS) });
    const stream = (await call(client, { model: 'gpt-5.5', messages: MESSAGES, stream: true })) as AsyncIterable<Chunk>;
    for await (const c of stream) {
      void c;
      break;
    }
    await monitor.drain();
    const [event] = eventsOf(monitor);
    expect(event.stream).toBe(true);
    expect(event.usage).toBeNull();
  });

  it('leaves existing stream_options alone and skips them for other hosts', async () => {
    const client = new FakeClient('http://localhost:8000/v1');
    const monitor = wrap(client, { app: 'shop', routeFn: makeRouteFn(SCORES_MINI_WINS) });
    await call(client, { model: 'gpt-5.5', messages: MESSAGES, stream: true });
    await monitor.drain();
    expect('stream_options' in client.chat.completions.calls[0]).toBe(false);
  });
});

// --- identity ---------------------------------------------------------------

describe('identity', () => {
  it('process precedence: per call > context > args > call site', async () => {
    const client = new FakeClient();
    const monitor = wrap(client, { app: 'shop', process: 'from-args', routeFn: makeRouteFn(SCORES_MINI_WINS) });
    await inProcess('from-context', async () => {
      await call(client, { model: 'gpt-5.5', messages: MESSAGES, tryaii_process: 'per-call' });
      await call(client, { model: 'gpt-5.5', messages: MESSAGES });
    });
    await call(client, { model: 'gpt-5.5', messages: MESSAGES });
    await monitor.drain();
    expect(eventsOf(monitor).map((e) => e.process)).toEqual(['per-call', 'from-context', 'from-args']);

    const bare = new FakeClient();
    const monitor2 = wrap(bare, { app: 'shop2', routeFn: makeRouteFn(SCORES_MINI_WINS) });
    await call(bare, { model: 'gpt-5.5', messages: MESSAGES });
    await monitor2.drain();
    const [event] = eventsOf(monitor2);
    expect(String(event.process)).toMatch(/^monitor-wrap\.test\.ts:/);
  });

  it('app comes from the environment, then the arguments', async () => {
    const monitor = wrap(new FakeClient(), { app: 'From Args!', routeFn: makeRouteFn(SCORES_MINI_WINS) });
    expect(monitor.app).toBe('from-args');
    process.env.TRYAII_APP = 'Env App';
    const monitor2 = wrap(new FakeClient(), { app: 'from-args', routeFn: makeRouteFn(SCORES_MINI_WINS) });
    expect(monitor2.app).toBe('env-app');
    expect(monitor2.dir.replace(/\\/g, '/')).toMatch(/\/monitor\/env-app$/);
  });

  it('records an unknown model with the top winner', async () => {
    const client = new FakeClient();
    const monitor = wrap(client, { app: 'shop', routeFn: makeRouteFn(SCORES_MINI_WINS) });
    await call(client, { model: 'my-finetune', messages: MESSAGES });
    await monitor.drain();
    const [event] = eventsOf(monitor);
    expect(event.model_resolved).toBeNull();
    expect(event.est_cost_usd).toBeNull();
    expect(event.winner).toBe('openai/gpt-5-mini');
  });

  it('is fail-open when the monitor itself breaks', async () => {
    const client = new FakeClient();
    const monitor = wrap(client, { app: 'shop', routeFn: makeRouteFn(SCORES_MINI_WINS) });
    (monitor as unknown as { before: () => null }).before = () => null;
    const resp = (await call(client, { model: 'gpt-5.5', messages: MESSAGES })) as { model: string };
    expect(resp.model).toBe('gpt-5.5');
    expect(eventsOf(monitor)).toEqual([]);
  });

  it('re-wrapping replaces the previous monitor', async () => {
    const client = new FakeClient();
    wrap(client, { app: 'one', routeFn: makeRouteFn(SCORES_MINI_WINS) });
    const monitor = wrap(client, { app: 'two', routeFn: makeRouteFn(SCORES_MINI_WINS) });
    await call(client, { model: 'gpt-5.5', messages: MESSAGES });
    await monitor.drain();
    expect(client.chat.completions.calls).toHaveLength(1);
    expect(eventsOf(monitor).map((e) => e.app)).toEqual(['two']);
  });

  it('install sets the defaults later wraps start from', async () => {
    install({ app: 'installed', mode: 'dynamic' });
    try {
      expect(defaults.app).toBe('installed');
      const client = new FakeClient();
      const monitor = wrap(client, { routeFn: makeRouteFn(SCORES_MINI_WINS) });
      expect(monitor.app).toBe('installed');
      await call(client, { model: 'gpt-5.5', messages: MESSAGES });
      await monitor.drain();
      expect(client.chat.completions.calls[0].model).toBe('gpt-5-mini');
    } finally {
      install();
    }
  });
});

// --- slice 2: managed mode ----------------------------------------------------

function setManaged(monitor: Monitor, process: string, model: string): void {
  store.writeConfig(monitor.dir, {
    schema: 'tryaii.monitor.config/1',
    source: 'local',
    default_mode: 'off',
    processes: { [process]: { mode: 'managed', model, providers: null } },
  });
}

describe('managed', () => {
  it('sends its model and still records the winner', async () => {
    const calls: string[] = [];
    const client = new FakeClient();
    const monitor = wrap(client, { app: 'shop', process: 'checkout', routeFn: makeRouteFn(SCORES_HAIKU_WINS, EMBEDDING, calls) });
    setManaged(monitor, 'checkout', 'openai/gpt-5-nano');
    await call(client, { model: 'gpt-5.5', messages: MESSAGES });
    await monitor.drain();
    expect(client.chat.completions.calls[0].model).toBe('gpt-5-nano');
    expect(calls).toHaveLength(1); // shadow only; no in-path routing
    const [event] = eventsOf(monitor);
    expect(event.mode).toBe('managed');
    expect(event.model_used).toBe('gpt-5-nano');
    expect(event.route_status).toBe('ok');
    expect(event.winner).toBe('anthropic/claude-haiku-4.5');
  });

  it('unreachable: sends the anchor with its status', async () => {
    const client = new FakeClient();
    const monitor = wrap(client, { app: 'shop', process: 'checkout', routeFn: makeRouteFn(SCORES_MINI_WINS) });
    setManaged(monitor, 'checkout', 'anthropic/claude-haiku-4.5');
    await call(client, { model: 'gpt-5.5', messages: MESSAGES });
    await monitor.drain();
    expect(client.chat.completions.calls[0].model).toBe('gpt-5.5');
    const [event] = eventsOf(monitor);
    expect(event.mode).toBe('managed');
    expect(event.model_used).toBe('gpt-5.5');
    expect(event.route_status).toBe('unreachable_managed');
    expect(event.winner).toBe('openai/gpt-5-mini'); // the recommendation is still recorded
  });

  it('without user text still sends the model', async () => {
    const client = new FakeClient();
    const monitor = wrap(client, { app: 'shop', process: 'checkout', routeFn: makeRouteFn(SCORES_MINI_WINS) });
    setManaged(monitor, 'checkout', 'openai/gpt-5-mini');
    await call(client, { model: 'gpt-5.5', messages: [{ role: 'system', content: 's' }] });
    await monitor.drain();
    expect(client.chat.completions.calls[0].model).toBe('gpt-5-mini');
    const [event] = eventsOf(monitor);
    expect(event.route_status).toBe('skipped');
  });

  it('resolves a native id', async () => {
    const client = new FakeClient();
    const monitor = wrap(client, { app: 'shop', process: 'checkout', routeFn: makeRouteFn(SCORES_MINI_WINS) });
    setManaged(monitor, 'checkout', 'gpt-5-nano');
    const resp = (await call(client, { model: 'gpt-5.5', messages: MESSAGES })) as { model: string };
    await monitor.drain();
    expect(resp.model).toBe('gpt-5-nano');
  });

  it('follows remote.json over config.json', async () => {
    const client = new FakeClient();
    const monitor = wrap(client, { app: 'shop', process: 'checkout', routeFn: makeRouteFn(SCORES_MINI_WINS) });
    setManaged(monitor, 'checkout', 'openai/gpt-5-mini');
    store.writeRemote(
      monitor.dir,
      {
        schema: 'tryaii.monitor.remote/1',
        etag: 'e-1',
        default_mode: 'off',
        processes: { checkout: { mode: 'managed', model: 'openai/gpt-5-nano', providers: null } },
      },
      '2026-10-11T12:00:00.000Z',
    );
    await call(client, { model: 'gpt-5.5', messages: MESSAGES });
    await monitor.drain();
    expect(client.chat.completions.calls[0].model).toBe('gpt-5-nano');
  });
});

// --- slice 2: policy-aware reachability (TS-170) --------------------------------

function policyMonitor(
  client: FakeClient,
  policy: readonly string[] | null,
  scores: unknown[],
  mode = 'dynamic',
  calls?: string[],
): Monitor {
  const transport = new Transport(null, { routeFn: makeRouteFn(scores, EMBEDDING, calls), inline: true, policy });
  transport.start();
  const monitor = new Monitor(providerClass(client.baseURL), { app: 'shop', process: 'p', mode, transport });
  const completions = client.chat.completions as unknown as { create: (body: Record<string, unknown>) => unknown };
  completions.create = makeCreate(monitor, completions.create.bind(client.chat.completions));
  return monitor;
}

describe('policy', () => {
  it('dynamic sends the best reachable model when the winner is unreachable', async () => {
    const scores = [
      { modelId: 'anthropic/claude-haiku-4.5', qualityScore: 0.81 },
      { modelId: 'openai/gpt-5-mini', qualityScore: 0.79 },
      { modelId: 'openai/gpt-5.5', qualityScore: 0.82 },
    ];
    const client = new FakeClient();
    const monitor = wrap(client, { app: 'shop', mode: 'dynamic', routeFn: makeRouteFn(scores) });
    await call(client, { model: 'gpt-5.5', messages: MESSAGES });
    await monitor.drain();
    expect(client.chat.completions.calls[0].model).toBe('gpt-5-mini');
    const [event] = eventsOf(monitor);
    expect(event.winner).toBe('anthropic/claude-haiku-4.5');
    expect(event.winner_reachable).toBe(false);
  });

  it('limits what is sent and what is recommended', async () => {
    const client = new FakeClient('https://openrouter.ai/api/v1');
    const monitor = policyMonitor(client, ['openai/gpt-5.5', 'openai/gpt-5-mini'], SCORES_HAIKU_WINS);
    await call(client, { model: 'openai/gpt-5.5', messages: MESSAGES });
    await monitor.drain();
    // haiku tops the ranking but the policy disallows it: never sent, never
    // recommended; gpt-5-mini is below the quality floor -> the anchor.
    expect(client.chat.completions.calls[0].model).toBe('openai/gpt-5.5');
    const [event] = eventsOf(monitor);
    expect(event.winner).toBe('openai/gpt-5.5');
  });

  it('allowing nothing sends the anchor and recommends nothing', async () => {
    const client = new FakeClient();
    const monitor = policyMonitor(client, [], SCORES_MINI_WINS);
    await call(client, { model: 'gpt-5.5', messages: MESSAGES });
    await monitor.drain();
    expect(client.chat.completions.calls[0].model).toBe('gpt-5.5');
    const [event] = eventsOf(monitor);
    expect(event.winner).toBeNull();
    expect(event.route_status).toBe('ok');
  });

  it('an openrouter client without a policy routes once', async () => {
    const calls: string[] = [];
    const client = new FakeClient('https://openrouter.ai/api/v1');
    const monitor = policyMonitor(client, null, SCORES_HAIKU_WINS, 'dynamic', calls);
    await call(client, { model: 'openai/gpt-5.5', messages: MESSAGES });
    await monitor.drain();
    expect(calls).toHaveLength(1); // in-path list == shadow list: the outcome is reused
  });

  it('another provider skips in-path routing', async () => {
    const calls: string[] = [];
    const client = new FakeClient('http://localhost:11434/v1');
    const monitor = policyMonitor(client, null, SCORES_MINI_WINS, 'dynamic', calls);
    await call(client, { model: 'gpt-5.5', messages: MESSAGES });
    await monitor.drain();
    expect(calls).toHaveLength(1); // only the shadow route: nothing but the anchor is sendable
  });

  it('a saved policy is resolved once, fails closed on the starter bundle, never touches the daemon', () => {
    savePolicy(new ModelPolicy({ origin: 'no-prc-models' }));
    const transport = new Transport(null, { routeFn: makeRouteFn(SCORES_MINI_WINS), inline: true });
    // The starter bundle has no origins.json: an active policy fails closed.
    expect(transport.policyAllowed()).toEqual([]);
    expect(transport.policyAllowed()).toEqual([]);
  });

  it('a broken saved policy fails closed', () => {
    writeFileSync(join(dataDir, 'policy.json'), '{broken', 'utf-8');
    const transport = new Transport(null, { routeFn: makeRouteFn(SCORES_MINI_WINS), inline: true });
    expect(transport.policyAllowed()).toEqual([]);
  });

  it('no saved policy is unconstrained', () => {
    const transport = new Transport(null, { routeFn: makeRouteFn(SCORES_MINI_WINS), inline: true });
    expect(transport.policyAllowed()).toBeNull();
  });

  it('registerApp reaches the services', () => {
    const transport = new Transport(null, { routeFn: makeRouteFn(SCORES_MINI_WINS), inline: true });
    const seen: string[] = [];
    transport.uploader = { register: (d: string) => seen.push(d), drain: async () => undefined } as never;
    const monitor = new Monitor('openai', { app: 'shop', transport });
    expect(seen).toEqual([monitor.dir]);
  });
});

describe('transport allowedModels', () => {
  it('the daemon gets allowedModels and the ranking is filtered anyway', async () => {
    const transport = new Transport(null, {
      routeFn: () => [
        [
          { modelId: 'anthropic/claude-haiku-4.5', qualityScore: 0.9 },
          { modelId: 'openai/gpt-5-mini', qualityScore: 0.8 },
        ],
        null,
        null,
      ],
      inline: true,
    });
    const out = await transport.route('hi', { includeEmbedding: false, allowedModels: ['openai/gpt-5-mini'] });
    expect(out.scores.map((s) => (s as { modelId: string }).modelId)).toEqual(['openai/gpt-5-mini']);
    const all = await transport.route('hi', { includeEmbedding: false });
    expect(all.scores).toHaveLength(2);
  });
});
