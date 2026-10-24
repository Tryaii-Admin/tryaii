/**
 * `wrap` for the `@anthropic-ai/sdk` SDK (TS-169). Mirrors
 * monitor/wrapper_anthropic.py.
 *
 * The Anthropic counterpart of `wrapOpenai.ts`, built on the same `Monitor`.
 * It replaces `client.messages.create` (and `client.messages.stream`, whose
 * `MessageStream` calls `create` itself) on the instance; duck-typed:
 * `@anthropic-ai/sdk` is never imported. The same `Messages` resource serves
 * `Anthropic`, `AnthropicBedrock` (`@anthropic-ai/bedrock-sdk`) and
 * `AnthropicVertex` (`@anthropic-ai/vertex-sdk`), so all of them are covered.
 *
 * Differences from the openai wrapper:
 *
 * - Routing text: the top-level `system` (a string or text blocks) is read as
 *   the system message, then the last `user` message.
 * - The event's `provider` is `anthropic` (Bedrock and Vertex clients too:
 *   they answer with the same Messages-API usage). It is not a SPEC.md §2.2
 *   class; routing treats it like `other`, so the label only tells the server
 *   that `prompt_tokens` excludes the cache (§3.1b). A Messages-API client is
 *   monitored and shadow-routed, but its model is never swapped. The mode's
 *   decision (which can only record, e.g. `unreachable_managed`) is taken
 *   alongside the request instead of before it, so `create()` always returns
 *   the SDK's own APIPromise (`.withResponse()`, `messages.stream()` and
 *   `messages.parse()` keep working).
 * - Usage (SPEC.md §3.1b, `anthropic` row): `prompt_tokens` = `input_tokens`
 *   as reported (it EXCLUDES cached and written tokens), `completion_tokens`
 *   = `output_tokens`, `total_tokens` = their sum when both are reported; the
 *   cache columns come from `normalizeCacheUsage('anthropic', ...)`. Nothing
 *   is derived that the response did not report.
 * - Streams are recorded from the `message_start` usage merged with the
 *   `message_delta` usage.
 * - Bedrock model ids (`us.anthropic.claude-...-v1:0`) resolve to the catalog
 *   anchor after stripping the region / vendor prefix and version suffix.
 */

import { performance } from 'node:perf_hooks';

import { normalizeCacheUsage, type Usage } from './events.js';
import { PROVIDER_ANTHROPIC, resolveAnchor, routeAllowed } from './winner.js';
import {
  Monitor,
  PER_CALL_PROCESS_KEY,
  defaults,
  wrap as wrapOpenai,
  type CallPlan,
  type FinishArgs,
  type MonitorOptions,
  type WrapOptions,
} from './wrapOpenai.js';
import { Transport } from './transport.js';

/** The `normalizeCacheUsage` shape of the Messages API. */
export const CACHE_SHAPE = 'anthropic';
/** Cache lifetimes a request's `cache_control.ttl` can ask for, shortest first. */
const TTL_ORDER = ['5m', '1h'] as const;
const BEDROCK_PREFIX = /^(?:[a-z]{2,6}(?:-[a-z]+)?\.)?anthropic\./;
const BEDROCK_SUFFIX = /-v\d+(?::\d+)?$/;

function debug(message: string, err?: unknown): void {
  if (process.env.TRYAII_VERBOSE) {
    process.stderr.write(`[tryaii monitor] ${message}${err ? `: ${String(err)}` : ''}\n`);
  }
}

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function get(obj: unknown, key: string): unknown {
  if (obj === null || obj === undefined || typeof obj !== 'object') return undefined;
  return (obj as Record<string, unknown>)[key];
}

function count(value: unknown): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return null;
  return Math.trunc(value);
}

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

/**
 * The event's `usage` (contract keys) from a Messages-API `usage`; null when
 * there is none. `input_tokens` is recorded unchanged as `prompt_tokens`
 * (SPEC.md §3.1b rule 2). Mirrors wrapper_anthropic.usage_from_anthropic.
 */
export function usageFromAnthropic(rawUsage: unknown): Usage | null {
  if (rawUsage === null || rawUsage === undefined || typeof rawUsage !== 'object') return null;
  const prompt = count(get(rawUsage, 'input_tokens'));
  const completion = count(get(rawUsage, 'output_tokens'));
  const total = prompt !== null && completion !== null ? prompt + completion : null;
  return {
    prompt_tokens: prompt,
    completion_tokens: completion,
    total_tokens: total,
    ...normalizeCacheUsage(CACHE_SHAPE, rawUsage),
    // API.md v1.7: the Messages API reports no reasoning share of output_tokens.
    reasoning_tokens: null,
  };
}

/**
 * Fold one stream event's usage into the running raw usage: `message_start`
 * first, then each `message_delta`, whose non-null fields (cumulative counts)
 * replace the earlier ones.
 */
export function mergeStreamUsage(
  current: Record<string, unknown> | null,
  update: unknown,
): Record<string, unknown> | null {
  if (!isObject(update)) return current;
  const merged: Record<string, unknown> = { ...(current ?? {}) };
  for (const [key, value] of Object.entries(update)) {
    if (value !== null && value !== undefined) merged[key] = value;
  }
  return merged;
}

/**
 * The request's `messages` with its top-level `system` (a string or text
 * blocks) put first as a `system` message, so routing text and the cache lint
 * read the Messages API like a chat-completions request.
 */
export function routingMessages(params: Record<string, unknown>): unknown {
  const messages = params.messages;
  const system = params.system;
  if (typeof system !== 'string' && !Array.isArray(system)) return messages;
  const first = { role: 'system', content: system };
  return Array.isArray(messages) ? [first, ...messages] : [first];
}

function ttlOf(block: unknown): string | null {
  const ttl = get(get(block, 'cache_control'), 'ttl');
  if (typeof ttl !== 'string') return null;
  const text = ttl.trim().toLowerCase();
  return (TTL_ORDER as readonly string[]).includes(text) ? text : null;
}

/**
 * The longest explicit `cache_control.ttl` the request asks for (top level,
 * `tools`, `system` blocks, message content blocks), or null when it names
 * none (SPEC.md §3.1b `cache_ttl_policy`).
 */
export function cacheTtlPolicy(params: Record<string, unknown>): string | null {
  const found: string[] = [];
  const visit = (block: unknown): void => {
    const ttl = ttlOf(block);
    if (ttl !== null) found.push(ttl);
  };
  visit(params);
  for (const key of ['tools', 'system']) {
    const value = params[key];
    if (Array.isArray(value)) value.forEach(visit);
  }
  if (Array.isArray(params.messages)) {
    for (const msg of params.messages) {
      visit(msg);
      const content = get(msg, 'content');
      if (Array.isArray(content)) content.forEach(visit);
    }
  }
  if (found.length === 0) return null;
  return found.reduce((a, b) =>
    TTL_ORDER.indexOf(b as (typeof TTL_ORDER)[number]) > TTL_ORDER.indexOf(a as (typeof TTL_ORDER)[number]) ? b : a,
  );
}

/**
 * A Bedrock id without its region / vendor prefix and version suffix
 * (`us.anthropic.claude-haiku-4-5-20251001-v1:0` → `claude-haiku-4-5-20251001`);
 * other ids unchanged.
 */
export function anchorModelId(model: string): string {
  const text = model.trim();
  const stripped = text.replace(BEDROCK_PREFIX, '');
  return stripped === text ? stripped : stripped.replace(BEDROCK_SUFFIX, '');
}

// ---------------------------------------------------------------------------
// Monitor
// ---------------------------------------------------------------------------

/** `Monitor` for a Messages-API client (event provider `anthropic`, routed like class `other`). */
export class AnthropicMonitor extends Monitor {
  constructor(opts: MonitorOptions = {}) {
    super(PROVIDER_ANTHROPIC, opts);
  }

  override before(params: Record<string, unknown>, perCallProcess: unknown, stream: boolean): CallPlan | null {
    let view: Record<string, unknown>;
    try {
      view = { model: params.model, messages: routingMessages(params) };
    } catch (err) {
      debug('anthropic before() failed', err);
      return null;
    }
    const plan = super.before(view, perCallProcess, stream);
    if (plan === null) return null;
    try {
      if (plan.anchor === null && plan.modelRequested) {
        const alt = anchorModelId(plan.modelRequested);
        if (alt !== plan.modelRequested) {
          const registry = this.transport.registry();
          const anchor = resolveAnchor(alt, registry);
          if (anchor !== null) {
            plan.anchor = anchor;
            plan.routeAllowed = routeAllowed(this.provider, registry.modelIds, anchor, plan.policy);
          }
        }
      }
      // The base read the chat-shaped view; Messages-API breakpoints sit on
      // tools, system and content blocks.
      plan.cacheTtlPolicy = cacheTtlPolicy(params);
    } catch (err) {
      debug('anthropic before() failed', err);
    }
    return plan;
  }

  /** The base step on a chat-shaped view (`system` first) for the cache lint. */
  override apply(plan: CallPlan, params: Record<string, unknown>): void {
    let view: Record<string, unknown>;
    try {
      view = { model: params.model, messages: routingMessages(params) };
      if (Array.isArray(params.tools)) view.tools = params.tools;
    } catch (err) {
      debug('anthropic apply() failed', err);
      return;
    }
    super.apply(plan, view);
    // A model rewrite is never chosen (only the anchor is reachable); copied back anyway.
    if ('model' in params && view.model !== params.model) params.model = view.model;
  }

  /** Messages-API usage: the `anthropic` shape (SPEC.md §3.1b). */
  override usageShape(): string {
    return CACHE_SHAPE;
  }

  override finish(plan: CallPlan, args: FinishArgs): void {
    let usage: Usage | null = null;
    try {
      usage = usageFromAnthropic(args.usage);
    } catch (err) {
      debug('anthropic usage failed', err);
    }
    super.finish(plan, { ...args, usage });
  }
}

// ---------------------------------------------------------------------------
// Stream wrapper
// ---------------------------------------------------------------------------

type Done = (usage: unknown, err: unknown) => void;

/**
 * A `stream: true` response: raw events forwarded unchanged; usage from
 * `message_start` merged with `message_delta`; the call recorded once the
 * stream ends (exhausted, returned early, or failed). `controller` is passed
 * through (MessageStream reads it). `tee()` is not supported.
 */
export class AnthropicMonitoredStream<T = unknown> implements AsyncIterable<T> {
  readonly controller: unknown;
  private usage: Record<string, unknown> | null = null;
  private finished = false;

  constructor(
    private readonly inner: AsyncIterable<T> & { controller?: unknown },
    private readonly done: Done,
  ) {
    this.controller = inner.controller;
  }

  private note(event: unknown): void {
    try {
      const kind = get(event, 'type');
      if (kind === 'message_start') this.usage = mergeStreamUsage(null, get(get(event, 'message'), 'usage'));
      else if (kind === 'message_delta') this.usage = mergeStreamUsage(this.usage, get(event, 'usage'));
    } catch (err) {
      debug('stream usage failed', err);
    }
  }

  private finish(err: unknown = null): void {
    if (this.finished) return;
    this.finished = true;
    try {
      this.done(this.usage, err);
    } catch (e) {
      debug('stream record failed', e);
    }
  }

  async *[Symbol.asyncIterator](): AsyncIterator<T> {
    try {
      for await (const event of this.inner) {
        this.note(event);
        yield event;
      }
      this.finish();
    } catch (err) {
      this.finish(err);
      throw err;
    } finally {
      this.finish(); // early return (break / return()) ends the record too
    }
  }
}

// ---------------------------------------------------------------------------
// The create() / stream() wrappers
// ---------------------------------------------------------------------------

type Fn = (this: unknown, body: Record<string, unknown>, options?: unknown) => unknown;

interface Wrapped extends Fn {
  __wrapped__?: Fn;
  __tryaii_monitor__?: Monitor;
}

interface ApiPromiseLike<T> extends Promise<T> {
  _thenUnwrap?: (transform: (data: T) => unknown) => Promise<unknown>;
}

function elapsedMs(t0: number): number {
  return Math.trunc(performance.now() - t0);
}

/** The plan `messages.stream()` captured for the `create()` it calls. */
let pendingPlan: CallPlan | null = null;

function runCall(
  monitor: AnthropicMonitor,
  original: Fn,
  target: unknown,
  plan: CallPlan,
  params: Record<string, unknown>,
  options: unknown,
): unknown {
  // The decision never changes the request of a Messages-API client:
  // it runs alongside the call and the record waits for it.
  const decided: Promise<void> | null = monitor.needsDecision(plan) ? monitor.decide(plan) : null;
  monitor.apply(plan, params);
  const t0 = performance.now();
  const record = (usage: unknown, error: unknown): void => {
    const latencyMs = elapsedMs(t0);
    if (decided === null) monitor.finish(plan, { usage, latencyMs, error });
    else void decided.then(() => monitor.finish(plan, { usage, latencyMs, error }));
  };
  let p: unknown;
  try {
    p = original.call(target, params, options);
  } catch (err) {
    record(null, err);
    throw err;
  }
  if (!p || typeof (p as Promise<unknown>).then !== 'function') {
    record(get(p, 'usage') ?? null, null); // not a promise (a mocked client)
    return p;
  }
  const promise = p as ApiPromiseLike<unknown>;
  if (plan.stream) {
    const toStream = (s: unknown): unknown =>
      new AnthropicMonitoredStream(s as AsyncIterable<unknown> & { controller?: unknown }, record);
    if (typeof promise._thenUnwrap === 'function') {
      // The SDK's APIPromise: keep it (`.withResponse()` is what
      // `messages.stream()` awaits) and swap only the parsed stream.
      const wrapped = promise._thenUnwrap(toStream);
      wrapped.then(undefined, (err: unknown) => record(null, err));
      return wrapped;
    }
    return promise.then(toStream, (err: unknown) => {
      record(null, err);
      throw err;
    });
  }
  // Side listeners only: the provider's own promise (APIPromise) is returned.
  promise.then(
    (resp) => record(get(resp, 'usage') ?? null, null),
    (err) => record(null, err),
  );
  return promise;
}

function splitBody(body: unknown): [Record<string, unknown>, unknown] {
  const params: Record<string, unknown> = isObject(body) ? { ...body } : {};
  const perCall = params[PER_CALL_PROCESS_KEY];
  delete params[PER_CALL_PROCESS_KEY];
  return [params, perCall];
}

/** The `messages.create` replacement (exported for tests). */
export function makeCreate(monitor: AnthropicMonitor, original: Fn): Wrapped {
  // A plain function: the call site and the process context are captured
  // synchronously, before the first await.
  const create = function (this: unknown, body: Record<string, unknown>, options?: unknown): unknown {
    const [params, perCall] = splitBody(body);
    let plan = pendingPlan;
    pendingPlan = null;
    if (plan === null) plan = monitor.before(params, perCall, Boolean(params.stream));
    if (plan === null) return original.call(this, isObject(body) ? params : body, options);
    return runCall(monitor, original, this, plan, params, options);
  } as Wrapped;
  create.__wrapped__ = original;
  create.__tryaii_monitor__ = monitor;
  return create;
}

/**
 * The `messages.stream` replacement: `MessageStream` calls `create()` from
 * inside the SDK (where no user frame is on the stack), so the plan is
 * captured here and handed to that `create()`, which runs synchronously.
 */
export function makeStream(monitor: AnthropicMonitor, original: Fn): Wrapped {
  const stream = function (this: unknown, body: Record<string, unknown>, options?: unknown): unknown {
    const [params, perCall] = splitBody(body);
    pendingPlan = monitor.before(params, perCall, true);
    try {
      return original.call(this, isObject(body) ? params : body, options);
    } finally {
      pendingPlan = null;
    }
  } as Wrapped;
  stream.__wrapped__ = original;
  stream.__tryaii_monitor__ = monitor;
  return stream;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

interface AnthropicClientLike {
  messages: { create: Fn; stream?: Fn } & Record<string, unknown>;
  chat?: unknown;
}

/** True for a Messages-API client (`client.messages.create`, no `client.chat`). */
export function isAnthropicClient(client: unknown): boolean {
  const messages = get(client, 'messages');
  return (
    messages !== null &&
    typeof messages === 'object' &&
    typeof get(messages, 'create') === 'function' &&
    (get(client, 'chat') === undefined || get(client, 'chat') === null)
  );
}

function unwrap(fn: Wrapped): Fn {
  return fn.__wrapped__ && fn.__tryaii_monitor__ ? fn.__wrapped__ : fn;
}

/**
 * Monitor every `client.messages.create` / `client.messages.stream` call of
 * an `@anthropic-ai/sdk` client (`Anthropic`, `AnthropicBedrock`,
 * `AnthropicVertex`). Options as `wrap`. Returns the monitor; the client is
 * patched in place.
 */
export function wrapAnthropic(client: unknown, opts: WrapOptions = {}): AnthropicMonitor {
  const messages = (client as AnthropicClientLike).messages;
  let transport: Transport | null = null;
  if (opts.routeFn || opts.inline) {
    transport = new Transport(null, {
      routeFn: opts.routeFn ?? null,
      inline: opts.inline === null || opts.inline === undefined ? true : opts.inline,
      policy: opts.policy,
    });
    transport.start();
  }
  const monitor = new AnthropicMonitor({
    app: opts.app ?? defaults.app,
    process: opts.process ?? defaults.process,
    mode: opts.mode ?? defaults.mode,
    transport,
    cachelint: Boolean(opts.cachelint) || defaults.cachelint,
    vectors: opts.vectors ?? null,
  });
  messages.create = makeCreate(monitor, unwrap(messages.create as Wrapped));
  if (typeof messages.stream === 'function') {
    messages.stream = makeStream(monitor, unwrap(messages.stream as Wrapped));
  }
  return monitor;
}

/**
 * Monitor every LLM call of `client`: an `@anthropic-ai/sdk` client goes to
 * `wrapAnthropic`, anything else (an `openai` client) to the openai wrapper.
 * `app` / `process` / `mode` set this client's defaults (SPEC.md §2.6).
 * `routeFn` and `inline` are the test seam: a function replacing the daemon,
 * run immediately. Returns the Monitor; the client is patched in place.
 */
export function wrap(client: unknown, opts: WrapOptions = {}): Monitor {
  return isAnthropicClient(client) ? wrapAnthropic(client, opts) : wrapOpenai(client, opts);
}
