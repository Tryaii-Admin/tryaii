import { describe, expect, it } from 'vitest';

import { Router, BaseEmbeddingProvider, Priorities } from '../src/index.js';

/**
 * Deterministic fake embedding provider.
 *
 * Reports the same modelName/dimension as the default `all-MiniLM-L6-v2`,
 * so the CentroidLoader finds the bundled centroid JSON. Vectors are hashed
 * from the prompt so runs are reproducible and two different prompts produce
 * two different vectors.
 *
 * Implements the sync `embed()` path; the async path is handled automatically
 * by BaseEmbeddingProvider's default `embedAsync()` wrapping. `supportsSync`
 * is overridden to true so this fake also exercises `Router.routeSync()`.
 */
class FakeEmbeddingProvider extends BaseEmbeddingProvider {
  embed(text: string): number[] {
    const seed = hashString(text);
    const vec = new Array<number>(384);
    let state = seed || 1;
    for (let i = 0; i < 384; i++) {
      // xorshift32 for a deterministic pseudo-random sequence
      state ^= state << 13;
      state ^= state >>> 17;
      state ^= state << 5;
      vec[i] = ((state >>> 0) / 0xffffffff) * 2 - 1;
    }
    // Normalize so cosine similarity behaves sanely
    let norm = 0;
    for (const v of vec) norm += v * v;
    norm = Math.sqrt(norm) || 1;
    for (let i = 0; i < 384; i++) vec[i] /= norm;
    return vec;
  }

  embedBatch(texts: string[]): number[][] {
    return texts.map((t) => this.embed(t));
  }

  get supportsSync(): boolean {
    return true;
  }

  get dimension(): number {
    return 384;
  }

  get modelName(): string {
    return 'all-MiniLM-L6-v2';
  }
}

/**
 * Async-only fake. Inherits the default-throwing `embed()` from
 * BaseEmbeddingProvider and implements only the async path -- used to verify
 * that `routeSync()` rejects async-only providers cleanly.
 */
class AsyncOnlyFakeProvider extends BaseEmbeddingProvider {
  private _sync = new FakeEmbeddingProvider();

  async embedAsync(text: string): Promise<number[]> {
    // Reuse the deterministic sync vector for stable test scores.
    return this._sync.embed(text);
  }

  get dimension(): number {
    return 384;
  }

  get modelName(): string {
    return 'all-MiniLM-L6-v2';
  }
}

/**
 * Fake provider with a controllable intrinsic-difficulty axis. Picks a fixed
 * unit "hard" direction (e0) and "easy" direction (e1). The 24 HARD exemplars
 * embed to e0, the 24 EASY exemplars to e1 (in classifier batch order), so the
 * hard/easy centroids land on those axes. A probe prompt tagged "HARD"/"EASY"
 * embeds toward the matching axis, letting us assert the difficulty ordering
 * without the real embedding model.
 */
class AxisDifficultyProvider extends BaseEmbeddingProvider {
  private _embedBatchCount = 0;

  embed(text: string): number[] {
    const v = new Array<number>(384).fill(0);
    if (text.includes('HARD')) v[0] = 1;
    else if (text.includes('EASY')) v[1] = 1;
    else v[2] = 1; // neutral, orthogonal to both axes
    return v;
  }

  // The classifier builds the difficulty centroids via embedBatchAsync:
  // EASY_EXEMPLARS first, then HARD_EXEMPLARS. First batch -> easy axis (e1),
  // second -> hard axis (e0). (The default embedBatchAsync loops embed(), so we
  // override the async batch path directly to control the centroid axes.)
  async embedBatchAsync(texts: string[]): Promise<number[][]> {
    const axis = this._embedBatchCount === 0 ? 1 : 0;
    this._embedBatchCount += 1;
    return texts.map(() => {
      const v = new Array<number>(384).fill(0);
      v[axis] = 1;
      return v;
    });
  }

  get dimension(): number {
    return 384;
  }

  get modelName(): string {
    return 'all-MiniLM-L6-v2';
  }
}

function hashString(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

function makeRouter(): Router {
  return new Router({ embeddingProvider: new FakeEmbeddingProvider() });
}

describe('Router', () => {
  it('loads the bundled default registry', () => {
    const router = new Router({ embeddingProvider: new FakeEmbeddingProvider() });
    expect(router.models.length).toBeGreaterThan(0);
  });

  it('routes through the embedding classifier', async () => {
    const router = makeRouter();
    const result = await router.route('Write a Python function to sort an array');

    expect(result.bestModel).toBeTruthy();
    expect(result.classification?.classifierUsed).toBe('embedding');
  });

  it('returns top-k scored models sorted descending', async () => {
    const router = makeRouter();
    const result = await router.route('Implement a binary search algorithm', { topK: 3 });

    expect(result.scores.length).toBeLessThanOrEqual(3);
    for (let i = 1; i < result.scores.length; i++) {
      expect(result.scores[i - 1].finalScore).toBeGreaterThanOrEqual(result.scores[i].finalScore);
    }
  });

  it('respects provider filters', async () => {
    const router = makeRouter();
    const result = await router.route('Write code', { filterProvider: 'Anthropic' });

    for (const score of result.scores) {
      const model = router.models.getModel(score.modelId);
      expect(model?.provider.toLowerCase()).toBe('anthropic');
    }
  });

  it('respects max-cost filters', async () => {
    const router = makeRouter();
    const result = await router.route('Write code', { filterMaxCost: 0.001 });

    for (const score of result.scores) {
      const model = router.models.getModel(score.modelId);
      if (model?.pricing) {
        expect(model.pricing.inputPer1k).toBeLessThanOrEqual(0.001);
      }
    }
  });

  it('returns an empty result when filters exclude every model', async () => {
    const router = makeRouter();
    const result = await router.route('Write code', { filterProvider: 'NonexistentProvider' });

    expect(result.bestModel).toBe('');
    expect(result.scores.length).toBe(0);
  });

  it('responds to different priorities', async () => {
    const router = makeRouter();
    const perf = await router.route('Debug this code', { priorities: Priorities.performance() });
    const budget = await router.route('Debug this code', { priorities: Priorities.budget() });

    expect(perf.scores.length).toBeGreaterThan(0);
    expect(budget.scores.length).toBeGreaterThan(0);
    // Either the winner differs or the top final score differs
    expect(
      perf.bestModel !== budget.bestModel ||
        perf.scores[0].finalScore !== budget.scores[0].finalScore,
    ).toBe(true);
  });

  it('reports non-zero confidence from the embedding classifier', async () => {
    const router = makeRouter();
    const result = await router.route('Explain quantum entanglement');

    expect(result.classification).not.toBeNull();
    expect(result.classification!.confidence).toBeGreaterThan(0);
  });
});

describe('Router.routeSync', () => {
  it('works with a sync-capable provider', () => {
    const router = makeRouter();
    const result = router.routeSync('Write a Python function to sort an array');

    expect(result.bestModel).toBeTruthy();
    expect(result.classification?.classifierUsed).toBe('embedding');
  });

  it('agrees with the async route() for the same prompt', async () => {
    const router = makeRouter();
    const sync = router.routeSync('Implement a binary search algorithm');
    const asyncResult = await makeRouter().route('Implement a binary search algorithm');

    expect(sync.bestModel).toBe(asyncResult.bestModel);
  });

  it('throws when the provider is async-only', () => {
    const router = new Router({ embeddingProvider: new AsyncOnlyFakeProvider() });
    expect(() => router.routeSync('Write some code')).toThrow(/async-only/);
  });
});

describe('Router.route with async-only provider', () => {
  it('works through the async path', async () => {
    const router = new Router({ embeddingProvider: new AsyncOnlyFakeProvider() });
    const result = await router.route('Write a Python function');

    expect(result.bestModel).toBeTruthy();
    expect(result.classification?.classifierUsed).toBe('embedding');
  });
});

describe('Router.addBenchmark', () => {
  const CUSTOM_NAME = 'CustomerSupportQA';
  const CUSTOM_QUERIES = [
    'How do I reset my password?',
    'I want to cancel my subscription',
    'Where is my order?',
    'Refund my last purchase please',
    'My account is locked, help',
  ];

  it('async: new benchmark appears in classification scores after route()', async () => {
    const router = makeRouter();
    await router.addBenchmark(CUSTOM_NAME, CUSTOM_QUERIES, 'Customer support queries');

    const result = await router.route('How do I change my billing address?');
    expect(result.classification).not.toBeNull();
    expect(result.classification!.benchmarkScores[CUSTOM_NAME]).toBeDefined();
    expect(result.classification!.benchmarkScores[CUSTOM_NAME]).toBeGreaterThanOrEqual(0);
  });

  it('async: works with an async-only provider (LocalEmbeddingProvider shape)', async () => {
    const router = new Router({ embeddingProvider: new AsyncOnlyFakeProvider() });
    await router.addBenchmark(CUSTOM_NAME, CUSTOM_QUERIES);

    const result = await router.route('Help me return a defective item');
    expect(result.classification!.benchmarkScores[CUSTOM_NAME]).toBeDefined();
  });

  it('async: classifier and router share the same centroid loader', async () => {
    // Regression test for the fresh-loader bug: previously addBenchmark
    // created a new CentroidLoader that the classifier never saw.
    const router = makeRouter();

    // Initialize the classifier before adding the benchmark.
    await router.route('warm up the classifier');

    await router.addBenchmark(CUSTOM_NAME, CUSTOM_QUERIES);

    const result = await router.route('Another support question');
    expect(Object.keys(result.classification!.benchmarkScores)).toContain(CUSTOM_NAME);
  });

  it('sync: works with a sync-capable provider', () => {
    const router = makeRouter();
    router.addBenchmarkSync(CUSTOM_NAME, CUSTOM_QUERIES);

    const result = router.routeSync('Another support question');
    expect(result.classification!.benchmarkScores[CUSTOM_NAME]).toBeDefined();
  });

  it('sync: throws when the provider is async-only', () => {
    const router = new Router({ embeddingProvider: new AsyncOnlyFakeProvider() });
    expect(() => router.addBenchmarkSync(CUSTOM_NAME, CUSTOM_QUERIES)).toThrow(/async-only/);
  });
});

describe('intrinsic difficulty (classifier)', () => {
  it('rates a hard-leaning prompt above an easy-leaning one, both in [0,1]', async () => {
    const router = new Router({ embeddingProvider: new AxisDifficultyProvider() });

    const hard = await router.route('HARD prompt');
    const easy = await router.route('EASY prompt');

    for (const d of [hard.classification?.difficulty, easy.classification?.difficulty]) {
      expect(d).toBeDefined();
      expect(d as number).toBeGreaterThanOrEqual(0);
      expect(d as number).toBeLessThanOrEqual(1);
    }
    // Hard-axis prompt sits closer to the HARD centroid -> higher difficulty.
    expect(hard.classification!.difficulty as number).toBeGreaterThan(
      easy.classification!.difficulty as number,
    );
  });

  it('gives a neutral (orthogonal) prompt difficulty ~0.5', async () => {
    const router = new Router({ embeddingProvider: new AxisDifficultyProvider() });
    const neutral = await router.route('something unrelated');
    // sim_hard == sim_easy == 0 -> sigmoid(0) = 0.5
    expect(neutral.classification!.difficulty as number).toBeCloseTo(0.5, 5);
  });
});
