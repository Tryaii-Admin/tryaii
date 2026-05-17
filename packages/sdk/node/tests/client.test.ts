/**
 * Smoke tests for DREClient.
 *
 * The real `tryaii-dre` core package is replaced by tests/fixtures/tryaii-dre-mock.ts
 * via the vitest resolve.alias in vitest.config.ts. These tests don't need
 * the core to be built or installed.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { DREClient } from '../src/client.js';
import {
  clearLastRouteCall,
  getLastRouteCall,
  resetFakeRouteResult,
  setFakeRouteResult,
} from './fixtures/tryaii-dre-mock.js';

// Build a fake SSE stream that matches OpenRouter's event format.
function makeSseStream(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) {
        const payload = JSON.stringify({ choices: [{ delta: { content: c } }] });
        controller.enqueue(encoder.encode(`data: ${payload}\n\n`));
      }
      controller.enqueue(encoder.encode('data: [DONE]\n\n'));
      controller.close();
    },
  });
}

beforeEach(() => {
  resetFakeRouteResult();
  clearLastRouteCall();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// route() -- delegation + _toSdkResult conversion
// ---------------------------------------------------------------------------

describe('DREClient.route()', () => {
  it('returns an SDK-shaped RouteResult built from the core result', async () => {
    const client = new DREClient({ apiKey: 'test-key' });
    const result = await client.route('Write a sorting algorithm', {
      priorities: { quality: 5, cost: 1, speed: 2 },
      topK: 3,
    });

    expect(result.bestModel).toBe('gpt-4o');
    // Core ModelScore has extra fields (qualityContribution, topBenchmarks) —
    // _toSdkResult must strip them down to the SDK ModelScore shape.
    expect(result.scores).toHaveLength(2);
    expect(result.scores[0]).toEqual({
      modelId: 'gpt-4o',
      finalScore: 0.92,
      qualityScore: 0.88,
      costScore: 0.4,
      speedScore: 0.4,
      reasoning: 'Quality: 0.88 on [MMLU (88%), HumanEval (90%)]',
    });
    expect(result.scores[0]).not.toHaveProperty('qualityContribution');
    expect(result.scores[0]).not.toHaveProperty('topBenchmarks');
    expect(result.bestScore).toBe(0.92);
    expect(result.bestReasoning).toMatch(/MMLU/);
    expect(result.priorities).toEqual({ quality: 5, cost: 1, speed: 2 });
  });

  it('forwards prompt, priorities, and topK to the core Router', async () => {
    const client = new DREClient({ apiKey: 'test-key' });
    await client.route('hello', {
      priorities: { quality: 4, cost: 2, speed: 1 },
      topK: 7,
    });

    const call = getLastRouteCall();
    expect(call).not.toBeNull();
    expect(call!.prompt).toBe('hello');
    expect(call!.opts.topK).toBe(7);
    // Priorities are passed through Priorities.fromDict — the fake returns the dict as-is.
    expect(call!.opts.priorities).toEqual({ quality: 4, cost: 2, speed: 1 });
  });

  it('returns bestScore=0 and empty strings when the core returns no scores', async () => {
    setFakeRouteResult({
      bestModel: '',
      scores: [],
      classification: null,
      priorities: { quality: 3, cost: 3, speed: 3 },
    });
    const client = new DREClient();
    const result = await client.route('anything');
    expect(result.bestModel).toBe('');
    expect(result.scores).toEqual([]);
    expect(result.bestScore).toBe(0);
    expect(result.bestReasoning).toBe('');
  });

  it('works without an API key (route() does not call OpenRouter)', async () => {
    const client = new DREClient(); // no apiKey
    await expect(client.route('hello')).resolves.toBeDefined();
  });
});

// ---------------------------------------------------------------------------
// chat() -- loud errors + OpenRouter HTTP
// ---------------------------------------------------------------------------

describe('DREClient.chat()', () => {
  it('throws when no API key is configured', async () => {
    const originalEnv = process.env.OPENROUTER_API_KEY;
    delete process.env.OPENROUTER_API_KEY;
    try {
      const client = new DREClient();
      await expect(client.chat('hello')).rejects.toThrow(/OpenRouter API key/);
    } finally {
      if (originalEnv !== undefined) process.env.OPENROUTER_API_KEY = originalEnv;
    }
  });

  it('throws when the core Router returns no model (instead of silently defaulting to gpt-4o)', async () => {
    setFakeRouteResult({
      bestModel: '',
      scores: [],
      classification: null,
      priorities: { quality: 3, cost: 3, speed: 3 },
    });
    const client = new DREClient({ apiKey: 'test-key' });
    await expect(client.chat('hello')).rejects.toThrow(
      /routing returned no model/,
    );
  });

  it('sends the OpenRouter-resolved slug and returns the parsed response', async () => {
    const fetchMock = vi.fn(async () =>
      new Response(
        JSON.stringify({
          choices: [{ message: { content: 'hi there' } }],
          usage: { prompt_tokens: 5, completion_tokens: 3, total_tokens: 8 },
        }),
        { status: 200, headers: { 'content-type': 'application/json' } },
      ),
    );
    vi.stubGlobal('fetch', fetchMock);

    const client = new DREClient({ apiKey: 'test-key' });
    const resp = await client.chat('hello world', { systemMessage: 'sys' });

    expect(resp.content).toBe('hi there');
    expect(resp.modelUsed).toBe('gpt-4o');
    expect(resp.openrouterModel).toBe('openai/gpt-4o');
    expect(resp.usage.promptTokens).toBe(5);
    expect(resp.usage.totalTokens).toBe(8);
    expect(resp.routeReasoning).toMatch(/MMLU/);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toContain('/chat/completions');
    expect((init.headers as Record<string, string>)['Authorization']).toBe('Bearer test-key');
    const body = JSON.parse(init.body as string);
    expect(body.model).toBe('openai/gpt-4o');
    expect(body.messages).toEqual([
      { role: 'system', content: 'sys' },
      { role: 'user', content: 'hello world' },
    ]);
  });

  it('surfaces OpenRouter HTTP errors with the status code', async () => {
    const fetchMock = vi.fn(async () =>
      new Response('rate limited', { status: 429 }),
    );
    vi.stubGlobal('fetch', fetchMock);

    const client = new DREClient({ apiKey: 'test-key' });
    await expect(client.chat('hello')).rejects.toThrow(/429/);
  });
});

// ---------------------------------------------------------------------------
// stream() -- loud errors + SSE parsing
// ---------------------------------------------------------------------------

describe('DREClient.stream()', () => {
  it('throws when no API key is configured', async () => {
    const originalEnv = process.env.OPENROUTER_API_KEY;
    delete process.env.OPENROUTER_API_KEY;
    try {
      const client = new DREClient();
      const gen = client.stream('hello');
      await expect(gen.next()).rejects.toThrow(/OpenRouter API key/);
    } finally {
      if (originalEnv !== undefined) process.env.OPENROUTER_API_KEY = originalEnv;
    }
  });

  it('throws when the core Router returns no model', async () => {
    setFakeRouteResult({
      bestModel: '',
      scores: [],
      classification: null,
      priorities: { quality: 3, cost: 3, speed: 3 },
    });
    const client = new DREClient({ apiKey: 'test-key' });
    const gen = client.stream('hello');
    await expect(gen.next()).rejects.toThrow(/routing returned no model/);
  });

  it('yields decoded content chunks from an SSE stream', async () => {
    const body = makeSseStream(['Hel', 'lo', ' world']);
    const fetchMock = vi.fn(async () =>
      new Response(body, {
        status: 200,
        headers: { 'content-type': 'text/event-stream' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);

    const client = new DREClient({ apiKey: 'test-key' });
    const out: string[] = [];
    for await (const chunk of client.stream('hello')) {
      out.push(chunk);
    }
    expect(out.join('')).toBe('Hello world');

    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    const body2 = JSON.parse(init.body as string);
    expect(body2.stream).toBe(true);
  });
});
