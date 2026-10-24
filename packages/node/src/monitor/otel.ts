/**
 * The active OpenTelemetry span at call time (SPEC.md "Langfuse export").
 * Mirrors monitor/otel.py.
 *
 * The wrapper records `{trace_id, span_id}` of the span that is active when
 * `create` is called (for example inside Langfuse's `startActiveObservation`
 * or any other OpenTelemetry instrumentation), so the Langfuse exporter can
 * put the TryAii span inside that trace. `@opentelemetry/api` is never a
 * dependency: it is loaded with `require` only when it resolves from here,
 * else the API's own global registration (`Symbol.for('opentelemetry.js.api.1')`,
 * set by whatever copy of the API the app uses) is read. Without either,
 * nothing is recorded. Never throws.
 */

import { createRequire } from 'node:module';

export interface OtelIds {
  trace_id: string;
  span_id: string;
}

interface SpanContextLike {
  traceId?: unknown;
  spanId?: unknown;
}

interface SpanLike {
  spanContext?: () => SpanContextLike;
}

interface TraceApi {
  getActiveSpan?: () => SpanLike | undefined;
}

const GLOBAL_KEY = Symbol.for('opentelemetry.js.api.1');
const SPAN_KEY = Symbol.for('OpenTelemetry Context Key SPAN');
const HEX32 = /^[0-9a-f]{32}$/;
const HEX16 = /^[0-9a-f]{16}$/;

let traceApi: TraceApi | null | undefined;

function loadTraceApi(): TraceApi | null {
  if (traceApi !== undefined) return traceApi;
  try {
    const req = createRequire(import.meta.url);
    const mod = req('@opentelemetry/api') as { trace?: TraceApi };
    traceApi = mod && mod.trace && typeof mod.trace.getActiveSpan === 'function' ? mod.trace : null;
  } catch {
    traceApi = null; // not installed next to tryaii: fall back to the global
  }
  return traceApi;
}

/** The active span read from the API's global context manager, if any. */
function globalActiveSpan(): SpanLike | undefined {
  const api = (globalThis as Record<symbol, unknown>)[GLOBAL_KEY] as
    | { context?: { active?: () => { getValue?: (key: symbol) => unknown } } }
    | undefined;
  const ctx = api?.context?.active?.();
  return ctx?.getValue?.(SPAN_KEY) as SpanLike | undefined;
}

/**
 * `{trace_id: <32 hex>, span_id: <16 hex>}` of the active span, or null (no
 * OpenTelemetry API, no active span, or an invalid one).
 */
export function activeSpanIds(): OtelIds | null {
  try {
    const api = loadTraceApi();
    const span = api !== null ? api.getActiveSpan?.() : globalActiveSpan();
    const sc = span?.spanContext?.();
    const traceId = sc?.traceId;
    const spanId = sc?.spanId;
    if (typeof traceId !== 'string' || typeof spanId !== 'string') return null;
    if (!HEX32.test(traceId) || !HEX16.test(spanId) || !/[^0]/.test(traceId) || !/[^0]/.test(spanId)) return null;
    return { trace_id: traceId, span_id: spanId };
  } catch {
    return null; // fail-open
  }
}

// --- Langfuse's observeOpenAI: the generation that wraps this call -----------
//
// `observeOpenAI` (@langfuse/openai) starts its generation before it calls the
// `create` TryAii patched behind the proxy (TS-301), and never makes it the
// active span; it reads no per-call parent either. So the wrapper cannot put
// a parent around it. Instead, while the Langfuse export is on, the tracer
// Langfuse starts its spans with (`getTracer('langfuse-sdk')`, from the
// OpenTelemetry global provider or Langfuse's isolated one) is watched: each
// span it starts is noted, read-only, for the rest of the synchronous tick.
// The `create` that Langfuse calls right after `startObservation` takes it, and
// the event's `otel` names that generation: the TryAii span becomes its child
// in the same trace (SPEC.md §11.2). Without a registered provider (no
// OpenTelemetry SDK, e.g. no NodeSDK / LangfuseSpanProcessor) nothing is
// watched and the call keeps the plain behaviour.

const LANGFUSE_GLOBAL_KEY = Symbol.for('langfuse');
const LANGFUSE_TRACER = 'langfuse-sdk';
const WATCHED = Symbol.for('tryaii.monitor.langfuseWatch');

let lastLangfuseSpan: OtelIds | null = null;

type Watchable = Record<string | symbol, unknown>;

function noteSpan(span: SpanLike | undefined): void {
  const sc = span?.spanContext?.();
  const traceId = sc?.traceId;
  const spanId = sc?.spanId;
  if (typeof traceId !== 'string' || typeof spanId !== 'string' || !HEX32.test(traceId) || !HEX16.test(spanId)) return;
  const ids = { trace_id: traceId, span_id: spanId };
  lastLangfuseSpan = ids;
  queueMicrotask(() => {
    if (lastLangfuseSpan === ids) lastLangfuseSpan = null; // only for this tick
  });
}

function watchTracer(tracer: unknown): void {
  const t = tracer as Watchable | null;
  if (t === null || typeof t !== 'object' || t[WATCHED] || typeof t.startSpan !== 'function') return;
  const original = t.startSpan as (...args: unknown[]) => SpanLike;
  t.startSpan = function (this: unknown, ...args: unknown[]): SpanLike {
    const span = original.apply(this, args);
    try {
      noteSpan(span);
    } catch {
      /* fail-open */
    }
    return span;
  };
  t[WATCHED] = true;
}

function watchProvider(provider: unknown): boolean {
  const p = provider as Watchable | null | undefined;
  if (!p || typeof p !== 'object' || typeof p.getTracer !== 'function') return false;
  if (p[WATCHED]) return true;
  const original = p.getTracer as (...args: unknown[]) => unknown;
  p.getTracer = function (this: unknown, ...args: unknown[]): unknown {
    const tracer = original.apply(this, args);
    try {
      if (args[0] === LANGFUSE_TRACER) watchTracer(tracer);
    } catch {
      /* fail-open */
    }
    return tracer;
  };
  p[WATCHED] = true;
  return true;
}

/**
 * Watch Langfuse's tracer (idempotent, cheap once installed). True when a
 * provider is watched. Never throws.
 */
export function watchLangfuseSpans(): boolean {
  try {
    const g = globalThis as Record<symbol, unknown>;
    const api = g[GLOBAL_KEY] as { trace?: unknown } | undefined;
    const isolated = (g[LANGFUSE_GLOBAL_KEY] as { isolatedTracerProvider?: unknown } | undefined)?.isolatedTracerProvider;
    const a = watchProvider(api?.trace);
    const b = watchProvider(isolated);
    return a || b;
  } catch {
    return false;
  }
}

/** The Langfuse span started in this synchronous tick (and forget it), or null. */
export function takeLangfuseSpan(): OtelIds | null {
  const ids = lastLangfuseSpan;
  lastLangfuseSpan = null;
  return ids;
}
