/**
 * Transport ordering against a fake daemon (TS-173): background shadow routes
 * wait for the warm-up, in-path routes never do, drain stays bounded.
 *
 * The daemon module (ensureDaemon / liveState / request) and the catalog
 * selection are mocked; no process is spawned and no socket is opened.
 */

import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { Monitor, Transport, clock, store } from '../src/monitor/index.js';
import { makeCreate } from '../src/monitor/wrapOpenai.js';
import type { DaemonState } from '../src/daemon.js';

const ctl = vi.hoisted(() => ({
  /** What ensureDaemon() does; replaced per test. */
  ensure: async (): Promise<unknown> => null,
  requests: [] as Record<string, unknown>[],
  liveStateCalls: 0,
  // Starter-catalog models: gpt-5-mini is the cheaper in-band winner for a gpt-5.5 anchor.
  scores: [
    { modelId: 'openai/gpt-5-mini', qualityScore: 0.8 },
    { modelId: 'openai/gpt-5.5', qualityScore: 0.82 },
    { modelId: 'anthropic/claude-haiku-4.5', qualityScore: 0.79 },
  ],
  classification: { benchmarkScores: {}, broadCategory: 'coding', subcategory: 'debugging', confidence: 0.8123 },
}));

vi.mock('../src/daemon.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/daemon.js')>();
  return {
    ...actual,
    isDisabled: () => false,
    ensureDaemon: () => ctl.ensure(),
    liveState: async () => {
      ctl.liveStateCalls += 1;
      return null;
    },
    request: async (_state: unknown, payload: Record<string, unknown>) => {
      ctl.requests.push(payload);
      return {
        ok: true,
        result: { bestModel: 'openai/gpt-5-mini', scores: ctl.scores, classification: ctl.classification },
      };
    },
  };
});

// These tests drive the daemon path, the fallback when the in-process engine
// (TS-253) is unavailable; tests/monitor-engine.test.ts covers the engine.
vi.mock('../src/monitor/engine.js', () => ({
  InProcessEngine: {
    create: async () => ({ engine: null, reason: 'onnxruntime-node is not installed' }),
  },
}));

vi.mock('../src/catalog/client.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/catalog/client.js')>();
  const { starterBundle } = await import('../src/catalog/bundle.js');
  return {
    ...actual,
    selectCatalog: async () => ({ bundle: starterBundle(), notice: null, checked: false }),
  };
});

const NOW = Date.UTC(2026, 9, 11, 12, 0, 0, 123);
const MESSAGES = [{ role: 'user', content: 'Fix the failing test in checkout.py' }];
const STATE: DaemonState = {
  runtime: 'node',
  version: 'test',
  embeddingModel: 'fake-embed',
  host: '127.0.0.1',
  port: 1,
  token: 't',
  pid: 1,
  startedAtMs: 0,
  features: [],
};

function gate(): { promise: Promise<void>; release: () => void } {
  let release: () => void = () => undefined;
  const promise = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { promise, release };
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** A Monitor on `transport` and a fake provider call it wraps. */
function monitored(transport: Transport, mode: string | null = null) {
  const sent: Record<string, unknown>[] = [];
  const monitor = new Monitor('openai', { app: 'shop', process: 'p', mode, transport, vectors: false });
  const create = makeCreate(monitor, (body: Record<string, unknown>) => {
    sent.push({ ...body });
    return Promise.resolve({
      model: body.model,
      usage: { prompt_tokens: 100, completion_tokens: 50, total_tokens: 150 },
    });
  }) as unknown as (body: Record<string, unknown>) => Promise<unknown>;
  return { monitor, sent, create };
}

let dataDir: string;
const savedNow = clock.now;
const savedEnv: Record<string, string | undefined> = {};

beforeEach(() => {
  dataDir = mkdtempSync(join(tmpdir(), 'tryaii-monitor-transport-'));
  for (const key of [
    'TRYAII_DRE_DATA_DIR',
    'TRYAII_MONITOR_MODE',
    'TRYAII_APP',
    'TRYAII_MONITOR_VECTORS',
    'TRYAII_API_KEY',
    'TRYAII_HTTP_FIXTURE',
  ]) {
    savedEnv[key] = process.env[key];
    delete process.env[key];
  }
  process.env.TRYAII_DRE_DATA_DIR = dataDir;
  clock.now = () => NOW;
  ctl.ensure = async () => null;
  ctl.requests = [];
  ctl.liveStateCalls = 0;
});

afterEach(() => {
  clock.now = savedNow;
  for (const [key, value] of Object.entries(savedEnv)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
  rmSync(dataDir, { recursive: true, force: true });
});

describe('background routes wait for the warm-up', () => {
  it('routes the first call after start through the daemon the warm-up found', async () => {
    ctl.ensure = async () => {
      await delay(100);
      return STATE;
    };
    const transport = new Transport();
    transport.start();
    const { monitor, create } = monitored(transport);

    await create({ model: 'gpt-5.5', messages: MESSAGES }); // returns before the warm-up settles
    await transport.drain(5000);

    const [event] = store.readRecentEvents(monitor.dir, 50);
    expect(event.route_status).toBe('ok');
    expect(event.winner).toBe('openai/gpt-5-mini');
    expect(ctl.requests).toHaveLength(1);
    expect(ctl.liveStateCalls).toBe(0); // never probed ahead of the warm-up
  });

  it('is fail-open: a failed warm-up still records the call as unavailable', async () => {
    ctl.ensure = async () => {
      await delay(50);
      throw new Error('spawn failed');
    };
    const transport = new Transport();
    transport.start();
    const { monitor, create } = monitored(transport);

    await create({ model: 'gpt-5.5', messages: MESSAGES });
    await transport.drain(5000);

    const [event] = store.readRecentEvents(monitor.dir, 50);
    expect(event.route_status).toBe('unavailable');
    expect(event.winner).toBeNull();
    expect(ctl.requests).toHaveLength(0);
  });

  it('drain resolves by its deadline while the warm-up is still running', async () => {
    const warm = gate();
    ctl.ensure = async () => {
      await warm.promise;
      return STATE;
    };
    const transport = new Transport();
    transport.start();
    const { monitor, create } = monitored(transport);
    await create({ model: 'gpt-5.5', messages: MESSAGES });

    const t0 = Date.now();
    await transport.drain(50);
    expect(Date.now() - t0).toBeLessThan(1000);
    expect(store.readRecentEvents(monitor.dir, 50)).toEqual([]); // still waiting, not recorded yet

    warm.release(); // the job records its event once the warm-up settles
    await transport.drain(5000);
    const [event] = store.readRecentEvents(monitor.dir, 50);
    expect(event.route_status).toBe('ok');
  });
});

describe('in-path routes never wait for the warm-up', () => {
  it('returns unavailable at once while the warm-up is pending', async () => {
    const warm = gate();
    ctl.ensure = async () => {
      await warm.promise;
      return STATE;
    };
    const transport = new Transport();
    transport.start();

    const result = await Promise.race([
      transport.route('hello', { includeEmbedding: false, inPath: true }),
      delay(500).then(() => 'waited' as const),
    ]);
    expect(result).not.toBe('waited');
    expect((result as { status: string }).status).toBe('unavailable');
    expect(ctl.requests).toHaveLength(0);

    warm.release();
    await transport.drain(5000);
    // Once the warm-up has found the daemon, the hot path routes through it.
    const after = await transport.route('hello', { includeEmbedding: false, inPath: true });
    expect(after.status).toBe('ok');
  });

  it('a dynamic call sends the anchor without waiting for a slow warm-up', async () => {
    const warm = gate();
    ctl.ensure = async () => {
      await warm.promise;
      return STATE;
    };
    const transport = new Transport();
    transport.start();
    const { monitor, sent, create } = monitored(transport, 'dynamic');

    const result = await Promise.race([
      create({ model: 'gpt-5.5', messages: MESSAGES }),
      delay(500).then(() => 'waited' as const),
    ]);
    expect(result).not.toBe('waited');
    expect(sent[0].model).toBe('gpt-5.5');

    warm.release();
    await transport.drain(5000);
    const [event] = store.readRecentEvents(monitor.dir, 50);
    expect(event.mode).toBe('dynamic');
    // The in-path list (openai/* ids) differs from the shadow list (no
    // policy: every model), so the recommendation comes from the background
    // shadow route, which waits for the warm-up (SPEC.md §2.8).
    expect(event.route_status).toBe('ok');
    expect(event.model_used).toBe('gpt-5.5');
  });
});

describe('allowedModels (TS-170)', () => {
  it('is sent to a daemon that advertises it, and the ranking is filtered anyway', async () => {
    ctl.ensure = async () => ({ ...STATE, features: ['allowedModels', 'routeEmbedding'] });
    const transport = new Transport();
    transport.start();
    await transport.drain(5000);
    const out = await transport.route('hi', { includeEmbedding: false, allowedModels: ['openai/gpt-5-mini'] });
    expect(ctl.requests.at(-1)?.allowedModels).toEqual(['openai/gpt-5-mini']);
    expect(out.scores.map((s) => (s as { modelId: string }).modelId)).toEqual(['openai/gpt-5-mini']);
    const all = await transport.route('hi', { includeEmbedding: false });
    expect('allowedModels' in (ctl.requests.at(-1) ?? {})).toBe(false);
    expect(all.scores).toHaveLength(3);
  });

  it('an older daemon gets no field; the client-side filter still applies', async () => {
    ctl.ensure = async () => STATE; // features: []
    const transport = new Transport();
    transport.start();
    await transport.drain(5000);
    const out = await transport.route('hi', { includeEmbedding: false, allowedModels: ['openai/gpt-5-mini'] });
    expect('allowedModels' in (ctl.requests.at(-1) ?? {})).toBe(false);
    expect(out.scores.map((s) => (s as { modelId: string }).modelId)).toEqual(['openai/gpt-5-mini']);
  });

  it('a dynamic openai call sends two requests: in-path (reachable) and shadow (all)', async () => {
    ctl.ensure = async () => ({ ...STATE, features: ['allowedModels'] });
    const transport = new Transport();
    transport.start();
    await transport.drain(5000);
    const { monitor, sent, create } = monitored(transport, 'dynamic');
    await create({ model: 'gpt-5.5', messages: MESSAGES });
    await transport.drain(5000);
    expect(sent[0].model).toBe('gpt-5-mini');
    expect(ctl.requests).toHaveLength(2);
    const inPath = ctl.requests[0].allowedModels as string[];
    expect(inPath.length).toBeGreaterThan(0);
    expect(inPath.every((m) => m.startsWith('openai/'))).toBe(true);
    expect('allowedModels' in ctl.requests[1]).toBe(false);
    const [event] = store.readRecentEvents(monitor.dir, 50);
    expect(event.winner).toBe('openai/gpt-5-mini');
  });
});

describe('exit while the warm-up is still running (TS-224)', () => {
  it('atExit records the pending calls as unavailable instead of losing them', async () => {
    const warm = gate();
    ctl.ensure = async () => {
      await warm.promise;
      return STATE;
    };
    const transport = new Transport();
    transport.start();
    const { monitor, create } = monitored(transport);
    await create({ model: 'gpt-5.5', messages: MESSAGES });
    await transport.drain(50);
    expect(store.readRecentEvents(monitor.dir, 50)).toEqual([]); // queued behind the warm-up

    const t0 = Date.now();
    await transport.atExit();
    expect(Date.now() - t0).toBeLessThan(4000);
    const [event] = store.readRecentEvents(monitor.dir, 50);
    expect(event.route_status).toBe('unavailable');
    expect(event.winner).toBeNull();
    expect(event.model_used).toBe('gpt-5.5');
    expect(ctl.requests).toHaveLength(0);
    warm.release();
  });
});
