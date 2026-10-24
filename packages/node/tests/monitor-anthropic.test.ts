/**
 * The anthropic wrapper (TS-169): Messages API create / stream.
 *
 * Duck-typed fakes and an injected routeFn (no SDK import, no daemon, no
 * network), like monitor-wrap.test.ts. One suite drives the real
 * `@anthropic-ai/sdk` over an in-memory fetch; it skips when the SDK is not
 * installed. Port of packages/python/tests/test_monitor_anthropic.py.
 */

import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  AnthropicMonitor,
  Monitor,
  anchorModelId,
  anthropicCacheTtlPolicy,
  clock,
  store,
  usageFromAnthropic,
  wrap,
} from '../src/monitor/index.js';
import { mergeStreamUsage, routingMessages } from '../src/monitor/wrapAnthropic.js';

const NOW = Date.UTC(2026, 9, 21, 9, 30, 0, 250);
const SYSTEM = 'You are terse.';
const MESSAGES = [{ role: 'user', content: 'Fix the failing test in checkout.py' }];
const CLASSIFICATION = { broadCategory: 'coding', subcategory: 'debugging', confidence: 0.8123 };
const EMBEDDING = { model: 'fake-embed', dim: 3, v: [0.1234, -0.5, 1] };
const SCORES = [
  { modelId: 'anthropic/claude-haiku-4.5', qualityScore: 0.81 },
  { modelId: 'anthropic/claude-sonnet-4.5', qualityScore: 0.82 },
  { modelId: 'openai/gpt-5-mini', qualityScore: 0.7 },
];
const RAW_USAGE = {
  input_tokens: 12,
  output_tokens: 40,
  cache_read_input_tokens: 2048,
  cache_creation_input_tokens: 1500,
  cache_creation: { ephemeral_5m_input_tokens: 1000, ephemeral_1h_input_tokens: 500 },
  server_tool_use: null,
  service_tier: 'standard',
};
const EXPECTED_USAGE = {
  prompt_tokens: 12,
  completion_tokens: 40,
  total_tokens: 52,
  cached_tokens: 2048,
  cache_write_tokens: 1500,
  cache_writes: [
    { tokens: 1000, ttl: '5m' },
    { tokens: 500, ttl: '1h' },
  ],
  reasoning_tokens: null,
};

// --- fakes ------------------------------------------------------------------

function streamEvents(): Record<string, unknown>[] {
  return [
    {
      type: 'message_start',
      message: {
        usage: {
          input_tokens: 12,
          output_tokens: 1,
          cache_read_input_tokens: 2048,
          cache_creation_input_tokens: 1500,
          cache_creation: { ephemeral_5m_input_tokens: 1000, ephemeral_1h_input_tokens: 500 },
        },
      },
    },
    { type: 'content_block_delta', delta: { text: 'a' } },
    { type: 'message_delta', usage: { output_tokens: 40, input_tokens: null, cache_read_input_tokens: null } },
    { type: 'message_stop' },
  ];
}

class FakeStream implements AsyncIterable<Record<string, unknown>> {
  controller = { signal: { aborted: false } };
  constructor(private readonly events: Record<string, unknown>[]) {}
  async *[Symbol.asyncIterator](): AsyncIterator<Record<string, unknown>> {
    for (const e of this.events) yield e;
  }
}

class OverloadedError extends Error {
  status = 529;
}

class FakeMessages {
  calls: Record<string, unknown>[] = [];
  constructor(private readonly opts: { raise?: Error | null; usage?: boolean } = {}) {}

  create(body: Record<string, unknown>): unknown {
    this.calls.push({ ...body });
    if (this.opts.raise) return Promise.reject(this.opts.raise);
    if (body.stream) return Promise.resolve(new FakeStream(streamEvents()));
    return Promise.resolve({ model: body.model, usage: this.opts.usage === false ? null : RAW_USAGE });
  }
}

class FakeAnthropic {
  messages: FakeMessages;
  constructor(
    messages?: FakeMessages,
    public baseURL = 'https://api.anthropic.com',
  ) {
    this.messages = messages ?? new FakeMessages();
  }
}

function makeRouteFn(calls?: string[]) {
  return (text: string): [unknown[], unknown, unknown] => {
    calls?.push(text);
    return [SCORES, CLASSIFICATION, EMBEDDING];
  };
}

function eventsOf(monitor: Monitor): Record<string, unknown>[] {
  return store.readRecentEvents(monitor.dir, 50);
}

function body(extra: Record<string, unknown> = {}): Record<string, unknown> {
  return { model: 'claude-sonnet-4-5', max_tokens: 256, system: SYSTEM, messages: MESSAGES, ...extra };
}

let dataDir: string;
const savedNow = clock.now;
const savedEnv: Record<string, string | undefined> = {};

beforeEach(() => {
  dataDir = mkdtempSync(join(tmpdir(), 'tryaii-monitor-anthropic-'));
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

// --- pure helpers --------------------------------------------------------------

describe('helpers', () => {
  it('maps tokens and every cache field', () => {
    expect(usageFromAnthropic(RAW_USAGE)).toEqual(EXPECTED_USAGE);
  });

  it('records only what was reported', () => {
    expect(usageFromAnthropic(null)).toBeNull();
    expect(usageFromAnthropic({ input_tokens: 5, output_tokens: 7 })).toEqual({
      prompt_tokens: 5, completion_tokens: 7, total_tokens: 12,
      cached_tokens: null, cache_write_tokens: null, cache_writes: null, reasoning_tokens: null,
    });
    const zero = usageFromAnthropic({
      input_tokens: 5, output_tokens: 7, cache_read_input_tokens: 0, cache_creation_input_tokens: 0,
      cache_creation: { ephemeral_5m_input_tokens: 0, ephemeral_1h_input_tokens: 0 },
    });
    expect(zero?.cached_tokens).toBe(0);
    expect(zero?.cache_write_tokens).toBe(0);
    expect(zero?.cache_writes).toEqual([]);
    expect(usageFromAnthropic({ input_tokens: 5 })?.total_tokens).toBeNull();
  });

  it('merges stream usage', () => {
    let merged = mergeStreamUsage(null, { input_tokens: 3, output_tokens: 1, cache_read_input_tokens: 9 });
    merged = mergeStreamUsage(merged, { output_tokens: 30, input_tokens: null });
    expect(merged).toEqual({ input_tokens: 3, output_tokens: 30, cache_read_input_tokens: 9 });
  });

  it('puts system first for routing', () => {
    const blocks = [{ type: 'text', text: 'A' }];
    expect((routingMessages({ system: blocks, messages: MESSAGES }) as unknown[])[0]).toEqual({
      role: 'system', content: blocks,
    });
    expect(routingMessages({ messages: MESSAGES })).toBe(MESSAGES);
  });

  it('reads the longest explicit cache ttl', () => {
    expect(anthropicCacheTtlPolicy({ messages: MESSAGES })).toBeNull();
    expect(anthropicCacheTtlPolicy({ system: [{ type: 'text', text: 's', cache_control: { type: 'ephemeral' } }] })).toBeNull();
    expect(
      anthropicCacheTtlPolicy({
        tools: [{ name: 't', cache_control: { type: 'ephemeral', ttl: '5m' } }],
        messages: [{ role: 'user', content: [{ type: 'text', text: 'x', cache_control: { type: 'ephemeral', ttl: '1h' } }] }],
      }),
    ).toBe('1h');
    expect(anthropicCacheTtlPolicy({ cache_control: { type: 'ephemeral', ttl: '5m' } })).toBe('5m');
  });

  it.each([
    ['us.anthropic.claude-haiku-4-5-20251001-v1:0', 'claude-haiku-4-5-20251001'],
    ['anthropic.claude-sonnet-4-5-20250929-v1:0', 'claude-sonnet-4-5-20250929'],
    ['global.anthropic.claude-sonnet-4-5-20250929-v1:0', 'claude-sonnet-4-5-20250929'],
    ['claude-haiku-4-5@20251001', 'claude-haiku-4-5@20251001'],
    ['claude-sonnet-4-5', 'claude-sonnet-4-5'],
  ])('strips Bedrock prefixes from %s', (model, expected) => {
    expect(anchorModelId(model)).toBe(expected);
  });
});

// --- create --------------------------------------------------------------------

describe('create', () => {
  it('labels the event anthropic, reads the anthropic shape and the request ttl', async () => {
    // provider `anthropic` tells the server prompt_tokens excludes the cache
    // (SPEC.md §3.1b); routing still treats it like class `other`.
    const client = new FakeAnthropic();
    const monitor = wrap(client, { app: 'shop', routeFn: makeRouteFn() }) as AnthropicMonitor;
    expect(monitor.provider).toBe('anthropic');
    expect(monitor.usageShape()).toBe('anthropic');
    await client.messages.create(
      body({ system: [{ type: 'text', text: SYSTEM, cache_control: { type: 'ephemeral', ttl: '1h' } }] }),
    );
    await monitor.drain();
    const [event] = eventsOf(monitor);
    expect(event.provider).toBe('anthropic');
    expect(event.cache_ttl_policy).toBe('1h');
    expect(event.usage).toEqual(EXPECTED_USAGE);
  });

  it('records an event without touching the call', async () => {
    const routed: string[] = [];
    const client = new FakeAnthropic();
    const monitor = wrap(client, { app: 'shop', process: 'checkout', routeFn: makeRouteFn(routed) });
    expect(monitor).toBeInstanceOf(AnthropicMonitor);
    const resp = (await client.messages.create(body({ tryaii_process: 'refunds' }))) as { model: string };
    await monitor.drain();

    expect(resp.model).toBe('claude-sonnet-4-5');
    const sent = client.messages.calls[0];
    expect('tryaii_process' in sent).toBe(false);
    expect('stream_options' in sent).toBe(false);
    expect(sent.system).toBe(SYSTEM);
    const [event] = eventsOf(monitor);
    expect(event.process).toBe('refunds');
    expect(event.provider).toBe('anthropic');
    expect(event.model_requested).toBe('claude-sonnet-4-5');
    expect(event.model_resolved).toBe('anthropic/claude-sonnet-4.5');
    expect(event.model_used).toBe('claude-sonnet-4-5');
    expect(event.mode).toBe('off');
    expect(event.route_status).toBe('ok');
    expect(event.winner).toBe('anthropic/claude-haiku-4.5');
    expect(event.winner_reachable).toBe(false); // never swapped on this client
    expect(event.usage).toEqual(EXPECTED_USAGE);
    expect(event.stream).toBe(false);
    expect(event.error).toBeNull();
    const site = event.call_site as Record<string, unknown>;
    expect(String(site.file).replace(/\\/g, '/')).toMatch(/monitor-anthropic\.test\.ts$/);
    expect(routed).toEqual([`${SYSTEM}\n\n${MESSAGES[0].content}`]);
  });

  it('never swaps the model in dynamic or managed mode', async () => {
    const client = new FakeAnthropic();
    const monitor = wrap(client, { app: 'shop', mode: 'dynamic', routeFn: makeRouteFn() });
    await client.messages.create(body());
    await new Promise((r) => setTimeout(r, 10)); // the record waits for the decision
    await monitor.drain();
    expect(client.messages.calls[0].model).toBe('claude-sonnet-4-5');
    expect(eventsOf(monitor)[0].mode).toBe('dynamic');

    const managed = new FakeAnthropic();
    const m2 = wrap(managed, { app: 'shop', routeFn: makeRouteFn() });
    store.writeConfig(m2.dir, {
      schema: 'tryaii.monitor.config/1', source: 'local', default_mode: 'off',
      processes: { p: { mode: 'managed', model: 'anthropic/claude-haiku-4.5', providers: null } },
    } as Parameters<typeof store.writeConfig>[1]);
    await managed.messages.create(body({ tryaii_process: 'p' }));
    await new Promise((r) => setTimeout(r, 10));
    await m2.drain();
    expect(managed.messages.calls[0].model).toBe('claude-sonnet-4-5');
    const last = eventsOf(m2).at(-1) as Record<string, unknown>;
    expect(last.mode).toBe('managed');
    expect(last.route_status).toBe('unreachable_managed');
  });

  it('resolves a Bedrock model id to its anchor', async () => {
    const client = new FakeAnthropic(undefined, 'https://bedrock-runtime.us-east-1.amazonaws.com');
    const monitor = wrap(client, { app: 'shop', routeFn: makeRouteFn() });
    await client.messages.create(body({ model: 'us.anthropic.claude-haiku-4-5-20251001-v1:0' }));
    await monitor.drain();
    const [event] = eventsOf(monitor);
    expect(event.model_used).toBe('us.anthropic.claude-haiku-4-5-20251001-v1:0');
    expect(event.model_resolved).toBe('anthropic/claude-haiku-4.5');
  });

  it('re-raises a provider error unchanged and records it', async () => {
    const err = new OverloadedError('busy');
    const client = new FakeAnthropic(new FakeMessages({ raise: err }));
    const monitor = wrap(client, { app: 'shop', routeFn: makeRouteFn() });
    await expect(client.messages.create(body())).rejects.toBe(err);
    await new Promise((r) => setTimeout(r, 10));
    await monitor.drain();
    const [event] = eventsOf(monitor);
    expect(event.error).toEqual({ status: 529, class: 'OverloadedError' });
    expect(event.usage).toBeNull();
  });

  it('records a missing usage as null', async () => {
    const client = new FakeAnthropic(new FakeMessages({ usage: false }));
    const monitor = wrap(client, { app: 'shop', routeFn: makeRouteFn() });
    await client.messages.create(body());
    await new Promise((r) => setTimeout(r, 10));
    await monitor.drain();
    expect(eventsOf(monitor)[0].usage).toBeNull();
  });

  it('forwards a stream and records it at the end', async () => {
    const client = new FakeAnthropic();
    const monitor = wrap(client, { app: 'shop', routeFn: makeRouteFn() });
    const stream = (await client.messages.create(body({ stream: true }))) as AsyncIterable<{ type: string }>;
    const kinds: string[] = [];
    for await (const e of stream) kinds.push(e.type);
    expect(kinds).toEqual(['message_start', 'content_block_delta', 'message_delta', 'message_stop']);
    await new Promise((r) => setTimeout(r, 10));
    await monitor.drain();
    const [event] = eventsOf(monitor);
    expect(event.stream).toBe(true);
    expect(event.usage).toEqual(EXPECTED_USAGE);
  });

  it('records a stream left early with what arrived', async () => {
    const client = new FakeAnthropic();
    const monitor = wrap(client, { app: 'shop', routeFn: makeRouteFn() });
    const stream = (await client.messages.create(body({ stream: true }))) as AsyncIterable<unknown>;
    for await (const _ of stream) break; // eslint-disable-line @typescript-eslint/no-unused-vars
    await new Promise((r) => setTimeout(r, 10));
    await monitor.drain();
    const usage = eventsOf(monitor)[0].usage as Record<string, unknown>;
    expect(usage.prompt_tokens).toBe(12);
    expect(usage.completion_tokens).toBe(1);
    expect(usage.cached_tokens).toBe(2048);
  });

  it('still dispatches openai clients', () => {
    const client = { baseURL: 'https://api.openai.com/v1', chat: { completions: { create: () => Promise.resolve({}) } } };
    const monitor = wrap(client, { app: 'shop', routeFn: makeRouteFn() });
    expect(monitor).not.toBeInstanceOf(AnthropicMonitor);
    expect(monitor.provider).toBe('openai');
  });

  it('re-wrapping replaces the previous monitor', async () => {
    const client = new FakeAnthropic();
    const first = wrap(client, { app: 'one', routeFn: makeRouteFn() });
    const second = wrap(client, { app: 'two', routeFn: makeRouteFn() });
    await client.messages.create(body());
    await new Promise((r) => setTimeout(r, 10));
    await second.drain();
    expect(eventsOf(first)).toEqual([]);
    expect(eventsOf(second)).toHaveLength(1);
    expect(client.messages.calls).toHaveLength(1);
  });
});

// --- the real SDK ------------------------------------------------------------------

type SdkModule = { default: new (opts: Record<string, unknown>) => Record<string, unknown> };
let sdk: SdkModule | null = null;
try {
  sdk = (await import('@anthropic-ai/sdk' as string)) as SdkModule;
} catch {
  sdk = null;
}

function sse(): string {
  const message = { id: 'msg_1', type: 'message', role: 'assistant', model: 'claude-sonnet-4-6', content: [], stop_reason: null, stop_sequence: null, usage: { ...RAW_USAGE, output_tokens: 1 } };
  const events: [string, unknown][] = [
    ['message_start', { type: 'message_start', message }],
    ['content_block_start', { type: 'content_block_start', index: 0, content_block: { type: 'text', text: '' } }],
    ['content_block_delta', { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: 'ok' } }],
    ['content_block_stop', { type: 'content_block_stop', index: 0 }],
    ['message_delta', { type: 'message_delta', delta: { stop_reason: 'end_turn', stop_sequence: null }, usage: { output_tokens: 40 } }],
    ['message_stop', { type: 'message_stop' }],
  ];
  return events.map(([k, v]) => `event: ${k}\ndata: ${JSON.stringify(v)}\n\n`).join('');
}

async function fakeFetch(_url: unknown, init: { body?: string }): Promise<Response> {
  const req = JSON.parse(String(init.body ?? '{}')) as Record<string, unknown>;
  if (req.stream) return new Response(sse(), { status: 200, headers: { 'content-type': 'text/event-stream' } });
  const message = { id: 'msg_1', type: 'message', role: 'assistant', model: 'claude-sonnet-4-6', content: [{ type: 'text', text: 'ok' }], stop_reason: 'end_turn', stop_sequence: null, usage: RAW_USAGE };
  return new Response(JSON.stringify(message), { status: 200, headers: { 'content-type': 'application/json' } });
}

describe.skipIf(sdk === null)('real @anthropic-ai/sdk', () => {
  it('monitors create, stream:true and messages.stream()', async () => {
    const Anthropic = (sdk as SdkModule).default;
    const client = new Anthropic({ apiKey: 'test', fetch: fakeFetch, maxRetries: 0 }) as {
      messages: {
        create(b: unknown): Promise<unknown> & { withResponse(): Promise<{ data: unknown }> };
        stream(b: unknown): { finalMessage(): Promise<{ usage: unknown }> };
      };
    };
    const monitor = wrap(client, { app: 'real', routeFn: makeRouteFn() });
    const kw = body({ model: 'claude-sonnet-4-6', max_tokens: 16 });
    const { data } = await client.messages.create(kw).withResponse(); // APIPromise kept
    expect((data as { id: string }).id).toBe('msg_1');
    const stream = (await client.messages.create({ ...kw, stream: true })) as AsyncIterable<unknown>;
    for await (const _ of stream); // eslint-disable-line @typescript-eslint/no-unused-vars
    const final = await client.messages.stream(kw).finalMessage();
    expect((final.usage as { output_tokens: number }).output_tokens).toBe(40);
    await new Promise((r) => setTimeout(r, 20));
    await monitor.drain();
    const events = eventsOf(monitor);
    expect(events.map((e) => e.stream)).toEqual([false, true, true]);
    for (const e of events) {
      expect(e.usage).toEqual(EXPECTED_USAGE);
      expect(e.provider).toBe('anthropic');
      expect(e.model_resolved).toBe('anthropic/claude-sonnet-4.6');
      const site = e.call_site as Record<string, unknown> | null;
      expect(String(site?.file).replace(/\\/g, '/')).toMatch(/monitor-anthropic\.test\.ts$/);
    }
  });
});
