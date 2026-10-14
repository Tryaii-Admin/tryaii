import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { createDefaultConfig, type TryaiiDreConfig } from '../src/config.js';
import * as daemon from '../src/daemon.js';
import { serve, type RouterLike } from '../src/server.js';
import type { RouteResult, RouteOptions } from '../src/router.js';
import { Priorities } from '../src/scoring/priorities.js';

/**
 * Daemon round-trip tests. They inject a fake router into the server so the
 * socket protocol, serialization, auth, and lifecycle are exercised without
 * the multi-second embedding-model load.
 */

function makeResult(priorities: Priorities): RouteResult {
  return {
    bestModel: 'gpt-5.2',
    scores: [
      {
        modelId: 'gpt-5.2',
        finalScore: 0.91,
        qualityScore: 0.8,
        costScore: 0.5,
        speedScore: 0.7,
        qualityContribution: 0.4,
        costContribution: 0.2,
        speedContribution: 0.31,
        topBenchmarks: [
          ['MMLU', 0.77],
          ['ARC', 0.66],
        ],
        reasoning: 'strong on reasoning',
      },
    ],
    classification: {
      benchmarkScores: { MMLU: 0.77, ARC: 0.66 },
      broadCategory: 'REASONING',
      subcategory: 'math',
      confidence: 0.42,
      classifierUsed: 'embedding',
      cacheHit: false,
      processingTimeMs: 1.5,
      difficulty: 0.3,
    },
    priorities,
  };
}

class FakeRouter implements RouterLike {
  async route(_prompt: string, opts?: RouteOptions): Promise<RouteResult> {
    return makeResult(opts?.priorities ?? new Priorities());
  }

  async embedPrompt(_prompt: string): Promise<number[]> {
    // Deterministic stand-in vector; 5 decimals so the wire rounding shows.
    return [0.123456, -0.5, 0.00004, 1.0];
  }
}

describe('routing daemon', () => {
  let config: TryaiiDreConfig;
  let state: daemon.DaemonState;
  let closed: Promise<void>;

  beforeEach(async () => {
    const dir = mkdtempSync(join(tmpdir(), 'tryaii-daemon-'));
    config = createDefaultConfig({ dataDir: dir });
    let resolveReady: (s: daemon.DaemonState) => void;
    const ready = new Promise<daemon.DaemonState>((resolve) => {
      resolveReady = resolve;
    });
    closed = serve(
      { embeddingModel: config.embeddingModel, dataDir: config.dataDir },
      { idleTimeout: 0, router: new FakeRouter(), log: () => {}, onReady: (s) => resolveReady(s) },
    );
    state = await ready;
  });

  afterEach(async () => {
    await daemon.stop(config);
    await closed;
  });

  it('ping reports runtime and embedding model', async () => {
    const info = await daemon.status(config);
    expect(info).not.toBeNull();
    expect(info?.ok).toBe(true);
    expect(info?.runtime).toBe('node');
    expect(info?.embeddingModel).toBe(config.embeddingModel);
  });

  it('route round-trip matches the in-process result', async () => {
    const priorities = new Priorities(5, 1, 1);
    const got = await daemon.routeViaDaemon(state, "what's greater 5 or 5.5?", priorities, 5);
    const expected = makeResult(priorities);

    expect(got.bestModel).toBe(expected.bestModel);
    expect(got.scores[0].modelId).toBe('gpt-5.2');
    expect(got.scores[0].finalScore).toBe(0.91);
    expect(got.scores[0].topBenchmarks).toEqual([
      ['MMLU', 0.77],
      ['ARC', 0.66],
    ]);
    expect(got.classification?.broadCategory).toBe('REASONING');
    expect(got.classification?.difficulty).toBe(0.3);
    expect(got.priorities.quality).toBe(5);
    expect(got.priorities.cost).toBe(1);
  });

  it('advertises the routeEmbedding feature in its state', () => {
    expect(daemon.supports(state, daemon.FEATURE_ROUTE_EMBEDDING)).toBe(true);
    expect(daemon.supports(state, 'somethingElse')).toBe(false);
    const { features: _omit, ...oldState } = state;
    expect(daemon.supports(oldState as daemon.DaemonState, daemon.FEATURE_ROUTE_EMBEDDING)).toBe(
      false,
    );
  });

  it('route with embedding returns the rounded vector', async () => {
    const { result, embedding } = await daemon.routeViaDaemonWithEmbedding(
      state,
      "what's greater 5 or 5.5?",
      new Priorities(),
      3,
    );
    expect(result.bestModel).toBe('gpt-5.2');
    expect(embedding).toEqual({
      model: state.embeddingModel,
      dim: 4,
      v: [0.1235, -0.5, 0, 1],
    });
    // The plain route path is unchanged: no embedding on the wire.
    const resp = await daemon.request(
      state,
      { cmd: 'route', prompt: 'x', priorities: {}, topK: 1 },
      5000,
    );
    expect(resp.ok).toBe(true);
    expect(resp.result as Record<string, unknown>).not.toHaveProperty('embedding');
  });

  it('rejects a non-boolean includeEmbedding', async () => {
    const resp = await daemon.request(
      state,
      { cmd: 'route', prompt: 'x', priorities: {}, topK: 1, includeEmbedding: 'yes' },
      5000,
    );
    expect(resp).toEqual({ ok: false, error: 'includeEmbedding must be a boolean' });
  });

  it('route with embedding refuses a daemon without the feature', async () => {
    const { features: _omit, ...oldState } = state;
    await expect(
      daemon.routeViaDaemonWithEmbedding(oldState as daemon.DaemonState, 'x', new Priorities(), 1),
    ).rejects.toThrow('does not support routeEmbedding');
  });

  it('rejects requests with a bad token', async () => {
    const resp = await daemon.request({ ...state, token: 'wrong-token' }, { cmd: 'ping' }, 5000);
    expect(resp.ok).toBe(false);
    expect(resp.error).toBe('unauthorized');
  });

  it('stop then status is null', async () => {
    expect(await daemon.stop(config)).toBe(true);
    await closed;
    expect(await daemon.status(config)).toBeNull();
    // Re-create the daemon so afterEach's stop/await has something defined.
    let resolveReady: (s: daemon.DaemonState) => void;
    const ready = new Promise<daemon.DaemonState>((resolve) => {
      resolveReady = resolve;
    });
    closed = serve(
      { embeddingModel: config.embeddingModel, dataDir: config.dataDir },
      { idleTimeout: 0, router: new FakeRouter(), log: () => {}, onReady: (s) => resolveReady(s) },
    );
    state = await ready;
  });
});

describe('daemon discovery guards', () => {
  it('ignores a state file from another runtime', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'tryaii-daemon-'));
    const config = createDefaultConfig({ dataDir: dir });
    daemon.writeState(config, {
      runtime: 'python',
      version: '0.3.0',
      embeddingModel: config.embeddingModel,
      host: '127.0.0.1',
      port: 1,
      token: 'x',
      pid: 0,
      startedAtMs: 0,
    });
    // A python state file must never be treated as a usable node daemon.
    expect(await daemon.liveState(config)).toBeNull();
  });
});
