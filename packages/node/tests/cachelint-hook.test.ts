/**
 * Tests for the cacheLint: 'warn' SDK hook (pre-flight lint + verification).
 *
 * Mirrors packages/python/tests/test_cachelint_hook.py — the shared-input
 * cases assert the SAME exact warning literals, which is how cross-SDK
 * message parity is enforced for the hook (no golden fixtures).
 *
 * Wiring tests stub globalThis.fetch by plain assignment (house style: no
 * vi.mock — hand-written fakes and constructor/attr injection only).
 */

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { buildCacheLintHook, CacheLintHook } from '../src/cachelint/hook.js';
import { OpenRouterIntegration } from '../src/integrations/openrouter.js';

const BIG = 'You are the routing assistant for a logistics platform. '.repeat(40);
const UUID = '7f9c02aa-51b3-4c2e-9f10-8a4d55e01b27';

// Shapes shared byte-for-byte with the Python suite.
const TINY_FABLE = ['anthropic/claude-fable-5', [{ role: 'user', content: 'Short prompt.' }]] as const;
const EARLY_UUID_FABLE = [
  'anthropic/claude-fable-5',
  [
    { role: 'system', content: `Session ${UUID}. ` + BIG },
    { role: 'user', content: 'Question?' },
  ],
] as const;
const CLEAN_DEEPSEEK = [
  'deepseek/deepseek-chat',
  [{ role: 'user', content: 'Hello there, tell me about Oslo.' }],
] as const;
const UUID_DEEPSEEK = [
  'deepseek/deepseek-chat',
  [{ role: 'user', content: `Continue session ${UUID}.` }],
] as const;
const CACHEABLE_GEMINI = [
  'google/gemini-2.5-pro',
  [
    { role: 'system', content: BIG.repeat(4) },
    { role: 'user', content: 'Q?' },
  ],
] as const;

// EXACT literals — byte-identical to the Python suite (except the note line's
// language-specific disable hint).
const BELOW_LINE =
  '[tryaii cachelint] BELOW_THRESHOLD for anthropic/claude-fable-5: ' +
  'Total prompt ~5 tok < 512 minimum — nothing will cache. ' +
  'Expanding the static content to reach the floor is often worthwhile.';
const NOTE_LINE =
  '[tryaii cachelint] note: reported once per unique prompt shape per client; ' +
  "set cacheLint: 'off' or TRYAII_CACHE_LINT=off to disable";
const BLOCKER_LINE =
  '[tryaii cachelint]   first blocker: uuid at messages[0]:system+16 (0% in)';
const VERIFY_MISS_LINE =
  '[tryaii cachelint] VERIFY_MISS for google/gemini-2.5-pro: predicted ~2244 tok ' +
  'cacheable prefix, but usage reports 0 cached tokens on repeat call ' +
  '#2 of this prompt shape — a silent invalidator or missing ' +
  'provider support may be the cause';

function makeHook(sink: string[], now?: () => number): CacheLintHook {
  return new CacheLintHook({ sink: (l) => sink.push(l), now });
}

describe('buildCacheLintHook mode resolution', () => {
  const saved = process.env.TRYAII_CACHE_LINT;
  afterEach(() => {
    if (saved === undefined) delete process.env.TRYAII_CACHE_LINT;
    else process.env.TRYAII_CACHE_LINT = saved;
  });

  it('explicit warn builds a hook; off/default do not', () => {
    delete process.env.TRYAII_CACHE_LINT;
    expect(buildCacheLintHook('warn')).toBeInstanceOf(CacheLintHook);
    expect(buildCacheLintHook('off')).toBeNull();
    expect(buildCacheLintHook(undefined)).toBeNull();
  });

  it('env enables when the option is unset, garbage is off, explicit off wins', () => {
    process.env.TRYAII_CACHE_LINT = 'WARN';
    expect(buildCacheLintHook(undefined)).toBeInstanceOf(CacheLintHook);
    process.env.TRYAII_CACHE_LINT = 'banana';
    expect(buildCacheLintHook(undefined)).toBeNull();
    process.env.TRYAII_CACHE_LINT = 'warn';
    expect(buildCacheLintHook('off')).toBeNull();
  });
});

describe('preflight', () => {
  it('warns BELOW_THRESHOLD with the exact literal', async () => {
    const sink: string[] = [];
    const hook = makeHook(sink);
    const key = await hook.preflight(TINY_FABLE[0], [...TINY_FABLE[1]]);
    expect(key).not.toBeNull();
    expect(sink).toEqual([BELOW_LINE, NOTE_LINE]);
  });

  it('warns EFFECTIVELY_UNCACHEABLE with the blocker line', async () => {
    const sink: string[] = [];
    const hook = makeHook(sink);
    await hook.preflight(EARLY_UUID_FABLE[0], [...EARLY_UUID_FABLE[1]]);
    expect(sink[0].startsWith(
      '[tryaii cachelint] EFFECTIVELY_UNCACHEABLE for anthropic/claude-fable-5:',
    )).toBe(true);
    expect(sink[1]).toBe(BLOCKER_LINE);
  });

  it('stays silent on clean UNKNOWN_THRESHOLD (KB gap, not a prompt problem)', async () => {
    const sink: string[] = [];
    const hook = makeHook(sink);
    expect(await hook.preflight(CLEAN_DEEPSEEK[0], [...CLEAN_DEEPSEEK[1]])).not.toBeNull();
    expect(sink).toEqual([]);
  });

  it('warns UNKNOWN_THRESHOLD when blockers exist', async () => {
    const sink: string[] = [];
    const hook = makeHook(sink);
    await hook.preflight(UUID_DEEPSEEK[0], [...UUID_DEEPSEEK[1]]);
    expect(sink[0].startsWith(
      '[tryaii cachelint] UNKNOWN_THRESHOLD for deepseek/deepseek-chat:',
    )).toBe(true);
    expect(sink.some((l) => l.includes('first blocker: uuid'))).toBe(true);
  });

  it('stays silent on cacheable shapes', async () => {
    const sink: string[] = [];
    const hook = makeHook(sink);
    expect(await hook.preflight(CACHEABLE_GEMINI[0], [...CACHEABLE_GEMINI[1]])).not.toBeNull();
    expect(sink).toEqual([]);
  });

  it('shows the note line exactly once across warning shapes', async () => {
    const sink: string[] = [];
    const hook = makeHook(sink);
    await hook.preflight(TINY_FABLE[0], [...TINY_FABLE[1]]);
    await hook.preflight(UUID_DEEPSEEK[0], [...UUID_DEEPSEEK[1]]);
    expect(sink.filter((l) => l === NOTE_LINE)).toHaveLength(1);
  });
});

describe('dedup', () => {
  it('analyzes and warns a shape once; repeats bump the counter', async () => {
    const sink: string[] = [];
    let analyzeCalls = 0;
    const hook = new CacheLintHook({
      sink: (l) => sink.push(l),
      loadEngine: async () => {
        const engine = await import('../src/cachelint/index.js');
        return {
          ...engine,
          analyze: (data: unknown) => {
            analyzeCalls++;
            return engine.analyze(data);
          },
        } as typeof engine;
      },
    });
    const k1 = await hook.preflight(TINY_FABLE[0], [...TINY_FABLE[1]]);
    const k2 = await hook.preflight(TINY_FABLE[0], [...TINY_FABLE[1]]);
    expect(k1).toBe(k2);
    expect(analyzeCalls).toBe(1);
    expect(sink.filter((l) => l === BELOW_LINE)).toHaveLength(1);
  });

  it('includes the model slug in the shape key', async () => {
    const hook = makeHook([]);
    const k1 = await hook.preflight('anthropic/claude-fable-5', [...TINY_FABLE[1]]);
    const k2 = await hook.preflight('openai/gpt-5.2', [...TINY_FABLE[1]]);
    expect(k1).not.toBe(k2);
  });

  it('stale shapes reset the call counter (no VERIFY_MISS on a fresh first call)', async () => {
    const sink: string[] = [];
    let t = 0;
    const hook = makeHook(sink, () => t);
    const key = await hook.preflight(CACHEABLE_GEMINI[0], [...CACHEABLE_GEMINI[1]]);
    t = 4_000_000; // > 1h stale window
    await hook.preflight(CACHEABLE_GEMINI[0], [...CACHEABLE_GEMINI[1]]);
    hook.verify(key, { cached_tokens: 0 });
    expect(sink).toEqual([]);
  });
});

describe('fail-open', () => {
  it('a rejected engine load makes the hook a permanent no-op', async () => {
    const sink: string[] = [];
    const hook = new CacheLintHook({
      sink: (l) => sink.push(l),
      loadEngine: () => Promise.reject(new Error('no engine')),
    });
    expect(await hook.preflight(TINY_FABLE[0], [...TINY_FABLE[1]])).toBeNull();
    expect(await hook.preflight(TINY_FABLE[0], [...TINY_FABLE[1]])).toBeNull();
    expect(sink).toEqual([]);
  });

  it('a throwing engine analyze is swallowed', async () => {
    const sink: string[] = [];
    const hook = new CacheLintHook({
      sink: (l) => sink.push(l),
      loadEngine: async () => {
        const engine = await import('../src/cachelint/index.js');
        return {
          ...engine,
          analyze: () => {
            throw new Error('boom');
          },
        } as typeof engine;
      },
    });
    expect(await hook.preflight(TINY_FABLE[0], [...TINY_FABLE[1]])).toBeNull();
    expect(sink).toEqual([]);
  });

  it('a broken sink is swallowed', async () => {
    const hook = new CacheLintHook({
      sink: () => {
        throw new Error('stderr is gone');
      },
    });
    expect(await hook.preflight(TINY_FABLE[0], [...TINY_FABLE[1]])).not.toBeNull();
  });

  it('verify never throws on junk', () => {
    const hook = makeHook([]);
    hook.verify('not-a-known-key', { bad: 'usage' });
    hook.verify(null, null);
    hook.verify('x', 42);
  });
});

describe('verify', () => {
  async function prime(hook: CacheLintHook, calls: number): Promise<string> {
    let key: string | null = null;
    for (let i = 0; i < calls; i++) {
      key = await hook.preflight(CACHEABLE_GEMINI[0], [...CACHEABLE_GEMINI[1]]);
    }
    return key as string;
  }

  it('first-call zero cache is the expected miss (silent)', async () => {
    const sink: string[] = [];
    const hook = makeHook(sink);
    const key = await prime(hook, 1);
    hook.verify(key, { prompt_tokens_details: { cached_tokens: 0 } });
    expect(sink).toEqual([]);
  });

  it('second-call zero cache warns with the exact literal', async () => {
    const sink: string[] = [];
    const hook = makeHook(sink);
    const key = await prime(hook, 2);
    hook.verify(key, { prompt_tokens_details: { cached_tokens: 0 } });
    expect(sink).toEqual([VERIFY_MISS_LINE]);
  });

  it('verify warns once per shape', async () => {
    const sink: string[] = [];
    const hook = makeHook(sink);
    const key = await prime(hook, 2);
    hook.verify(key, { cached_tokens: 0 });
    await hook.preflight(CACHEABLE_GEMINI[0], [...CACHEABLE_GEMINI[1]]);
    hook.verify(key, { cached_tokens: 0 });
    expect(sink).toHaveLength(1);
  });

  it('positive cache and discounts are silent', async () => {
    const sink: string[] = [];
    const hook = makeHook(sink);
    const key = await prime(hook, 2);
    hook.verify(key, { prompt_tokens_details: { cached_tokens: 1800 } });
    hook.verify(key, { cache_discount: 0.42 });
    expect(sink).toEqual([]);
  });

  it('unverifiable usage is silent', async () => {
    const sink: string[] = [];
    const hook = makeHook(sink);
    const key = await prime(hook, 2);
    hook.verify(key, {});
    hook.verify(key, null);
    hook.verify(key, { prompt_tokens_details: 'not-an-object' });
    hook.verify(key, { cached_tokens: true });
    expect(sink).toEqual([]);
  });

  it('uncacheable shapes are never verified', async () => {
    const sink: string[] = [];
    const hook = makeHook(sink);
    let key: string | null = null;
    for (let i = 0; i < 2; i++) key = await hook.preflight(TINY_FABLE[0], [...TINY_FABLE[1]]);
    sink.length = 0;
    hook.verify(key, { cached_tokens: 0 });
    expect(sink).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Wiring: the integration calls the hook around a stubbed fetch
// ---------------------------------------------------------------------------

const CHAT_BODY = {
  choices: [{ message: { content: 'Oslo is served by OSL-2.' } }],
  usage: { prompt_tokens_details: { cached_tokens: 0 }, total_tokens: 42 },
};

describe('integration wiring', () => {
  const realFetch = globalThis.fetch;
  beforeEach(() => {
    globalThis.fetch = (async () =>
      new Response(JSON.stringify(CHAT_BODY), { status: 200 })) as typeof fetch;
  });
  afterEach(() => {
    globalThis.fetch = realFetch;
  });

  function makeIntegration(sink: string[]): OpenRouterIntegration {
    const integ = new OpenRouterIntegration(null, { apiKey: 'k', cacheLint: 'warn' });
    // Swap in a collecting sink (attr injection, mirrors the Python suite).
    (integ as unknown as { _cacheLint: CacheLintHook })._cacheLint = new CacheLintHook({
      sink: (l) => sink.push(l),
    });
    return integ;
  }

  it('chat warns pre-flight and still succeeds', async () => {
    const sink: string[] = [];
    const integ = makeIntegration(sink);
    const resp = await integ.chat('Short prompt.', { overrideModel: 'anthropic/claude-fable-5' });
    expect(resp.content).toBe('Oslo is served by OSL-2.');
    expect(sink).toContain(BELOW_LINE);
  });

  it('chat succeeds even with a broken engine', async () => {
    const sink: string[] = [];
    const integ = new OpenRouterIntegration(null, { apiKey: 'k', cacheLint: 'warn' });
    (integ as unknown as { _cacheLint: CacheLintHook })._cacheLint = new CacheLintHook({
      sink: (l) => sink.push(l),
      loadEngine: () => Promise.reject(new Error('no engine')),
    });
    const resp = await integ.chat('Short prompt.', { overrideModel: 'anthropic/claude-fable-5' });
    expect(resp.content).toBe('Oslo is served by OSL-2.');
    expect(sink).toEqual([]);
  });

  it('cacheLint off is completely silent and adds no hook', async () => {
    const integ = new OpenRouterIntegration(null, { apiKey: 'k' });
    expect((integ as unknown as { _cacheLint: unknown })._cacheLint).toBeNull();
    const resp = await integ.chat('Short prompt.', { overrideModel: 'anthropic/claude-fable-5' });
    expect(resp.content).toBe('Oslo is served by OSL-2.');
  });

  it('repeat cacheable chat triggers VERIFY_MISS', async () => {
    const sink: string[] = [];
    const integ = makeIntegration(sink);
    for (let i = 0; i < 2; i++) {
      await integ.chat('Q?', {
        overrideModel: 'google/gemini-2.5-pro',
        systemMessage: BIG.repeat(4),
      });
    }
    expect(sink.some((l) => l.includes('VERIFY_MISS for google/gemini-2.5-pro'))).toBe(true);
  });

  it('stream verifies from a usage-bearing final SSE chunk', async () => {
    const sink: string[] = [];
    const sse = [
      'data: {"choices":[{"delta":{"content":"Hello"}}]}\n',
      'data: {"choices":[],"usage":{"prompt_tokens_details":{"cached_tokens":0}}}\n',
      'data: [DONE]\n',
    ].join('');
    globalThis.fetch = (async () =>
      new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(new TextEncoder().encode(sse));
            controller.close();
          },
        }),
        { status: 200 },
      )) as typeof fetch;

    const integ = makeIntegration(sink);
    // Prime the shape so the streamed call is call #2 (verify only fires then).
    const hook = (integ as unknown as { _cacheLint: CacheLintHook })._cacheLint;
    await hook.preflight('google/gemini-2.5-pro', [
      { role: 'system', content: BIG.repeat(4) },
      { role: 'user', content: 'Q?' },
    ]);

    const chunks: string[] = [];
    for await (const c of integ.stream('Q?', {
      overrideModel: 'google/gemini-2.5-pro',
      systemMessage: BIG.repeat(4),
    })) {
      chunks.push(c);
    }
    expect(chunks).toEqual(['Hello']);
    expect(sink.some((l) => l.includes('VERIFY_MISS'))).toBe(true);
  });
});
