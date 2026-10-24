/**
 * `wrap` / `install` for the `openai` SDK. Mirrors monitor/wrapper_openai.py.
 *
 * The wrapper replaces `client.chat.completions.create` on the instance
 * (duck-typed: `openai` is never imported here). Per call it
 *
 * 1. captures the call site and the sub-process name synchronously, before
 *    any await (`before`);
 * 2. decides what to send: `off` keeps the developer's model, `stable` uses
 *    the pinned model, `dynamic` routes in-path, `managed` sends the
 *    assigned model when it is reachable and policy-allowed (`decide`);
 * 3. calls the original method OUTSIDE monitor code - provider errors
 *    propagate unchanged after an error event;
 * 4. hands usage, latency and the plan to the transport, which shadow-routes
 *    (when not already routed) and writes the event and vector lines
 *    (`finish`).
 *
 * Every monitor step is fail-open: an exception inside it is swallowed
 * (debug output only) and the developer's call proceeds as written.
 *
 * In `off` mode a non-stream call returns the provider's own promise
 * (openai's APIPromise, so `.withResponse()` keeps working); `stable`,
 * `dynamic` and `managed` calls, and every stream call, return a plain
 * Promise.
 * `Stream.tee()` is not supported on a monitored stream.
 */

import { statSync } from 'node:fs';
import { basename, join } from 'node:path';
import { performance } from 'node:perf_hooks';

import { captureCallSite } from '../callsite.js';
import { relativeFile } from './relpath.js';
import { envFromOs, resolveApp, resolveMode, resolveProcess } from './config.js';
import { currentProcess } from './context.js';
import {
  buildEvent,
  buildVector,
  cacheTtlPolicyFromRequest,
  classificationFromWire,
  estimateCost,
  formatTs,
  usageFromProvider,
  type CallSiteRecord,
  type ErrorRecord,
  type Usage,
} from './events.js';
import { routingText } from './extract.js';
import { activeSpanIds, takeLangfuseSpan, watchLangfuseSpans, type OtelIds } from './otel.js';
import { settings as langfuseSettings } from './langfuse.js';
import { CacheTracker, type CacheBlock } from './cache.js';
import {
  evaluatePin,
  modelFacts,
  pinFromFirstCall,
  pinValid,
  setPin,
  type ModelFactsMap,
  type Pin,
  type PinsDoc,
} from './pins.js';
import * as store from './store.js';
import {
  STATUS_QUEUE_FULL,
  STATUS_SKIPPED,
  Transport,
  getTransport,
  outcome as makeOutcome,
  outcomeOk,
  type RouteOutcome,
  type RouteFn,
  type TransportPolicy,
} from './transport.js';
import {
  MODE_DYNAMIC,
  MODE_MANAGED,
  MODE_OFF,
  MODE_STABLE,
  PROVIDER_OPENAI,
  PROVIDER_OPENROUTER,
  managedTarget,
  nativeModelId,
  pickWinner,
  providerClass,
  reachable,
  resolveAnchor,
  routeAllowed,
  routedModel,
  shadowAllowed,
  type EventProvider,
  type Mode,
  type Provider,
} from './winner.js';
import type { ConfigDoc } from './config.js';

export const ENV_VECTORS = 'TRYAII_MONITOR_VECTORS';
export const DOC_RECHECK_MS = 5000;
export const PIN_HISTORY_S = 7 * 24 * 3600;
export const PER_CALL_PROCESS_KEY = 'tryaii_process';

/** The wrapper's clock (tests replace `now`). Epoch milliseconds. */
export const clock = {
  now(): number {
    return Date.now();
  },
};

export function vectorsEnabled(): boolean {
  return (process.env[ENV_VECTORS] ?? '1').trim() !== '0';
}

function debug(message: string, err?: unknown): void {
  if (process.env.TRYAII_VERBOSE) {
    process.stderr.write(`[tryaii monitor] ${message}${err ? `: ${String(err)}` : ''}\n`);
  }
}

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/**
 * A JSON document re-read when its mtime changes, checked at most every 5 s
 * (so `tryaii monitor set` applies without a restart). The reader may
 * return null (remote.json absent or invalid).
 *
 * `version` (optional) is an in-process change counter: when it moves, the
 * document is re-read on the next `get()` regardless of the 5 s window or
 * the mtime. remote.json uses it, so an answer the long poll just wrote
 * applies on the very next call (TS-296).
 */
class CachedDoc<T> {
  private doc: T | null = null;
  private loaded = false;
  private mtime: number | null = null;
  private seen: number | null = null;
  private checkedAt = -Infinity;

  constructor(
    private readonly directory: string,
    private readonly reader: (directory: string) => T,
    private readonly stat: (directory: string) => number | null,
    private readonly version: (() => number) | null = null,
  ) {}

  get(): T {
    const version = this.version !== null ? this.version() : null;
    const now = performance.now();
    if (!this.loaded || version !== this.seen) {
      this.reload(now, version);
    } else if (now - this.checkedAt >= DOC_RECHECK_MS) {
      this.checkedAt = now;
      if (this.stat(this.directory) !== this.mtime) this.reload(now, version);
    }
    return this.doc as T;
  }

  private reload(now: number, version: number | null): void {
    this.seen = version;
    this.checkedAt = now;
    this.mtime = this.stat(this.directory);
    this.doc = this.reader(this.directory);
    this.loaded = true;
  }

  set(doc: T): void {
    this.doc = doc;
    this.loaded = true;
    this.mtime = this.stat(this.directory);
    if (this.version !== null) this.seen = this.version();
    this.checkedAt = performance.now();
  }
}

function pinsMtime(directory: string): number | null {
  try {
    return statSync(join(directory, store.PINS_FILE)).mtimeMs / 1000;
  } catch {
    return null;
  }
}

/** Everything decided about one call before the provider is called. */
export interface CallPlan {
  ts: string;
  app: string;
  process: string;
  mode: Mode;
  provider: EventProvider;
  modelRequested: string | null;
  anchor: string | null;
  text: string;
  promptHash: string;
  callSite: CallSiteRecord | null;
  stream: boolean;
  /** Catalog id to send instead of the anchor (null = as written). */
  target: string | null;
  /** Native id put in the request. */
  modelSent: string | null;
  /**
   * The shadow route's outcome (the recommendation); set early only when the
   * in-path route used the same candidate list.
   */
  outcome: RouteOutcome | null;
  /** The in-path route's outcome (dynamic / first stable call). */
  routedOutcome: RouteOutcome | null;
  /** Managed model as configured (resolved in decide()). */
  managedModel: string | null;
  /** Where `mode` came from (SPEC.md §2.6: env, remote, args, config, default). */
  modeSource?: string | null;
  /** Overrides the route status in the event (`unreachable_managed`). */
  statusOverride: string | null;
  /**
   * Policy-allowed ids (null = no policy), and the candidate lists of the
   * in-path and the shadow route requests (SPEC.md §2.8).
   */
  policy: string[] | null;
  routeAllowed: string[] | null;
  shadowAllowed: string[] | null;
  lintKey: string | null;
  /**
   * The linted request (model and a snapshot of its messages), kept for the
   * event's `cache` block (API.md v1.4 §3.2.1); null when cachelint is off.
   */
  lintModel?: string | null;
  lintMessages?: unknown[] | null;
  /** Tool schemas of the linted request (`tools` then legacy `functions`). */
  lintTools?: unknown[] | null;
  /** The cache lifetime the request asked for (SPEC.md §3.1b), or null. */
  cacheTtlPolicy?: string | null;
  /** The active OpenTelemetry span at call time, or null (SPEC.md "Langfuse export"). */
  otel?: OtelIds | null;
}

function sameList(a: readonly string[] | null, b: readonly string[] | null): boolean {
  if (a === null || b === null) return a === b;
  return a.length === b.length && a.every((v, i) => v === b[i]);
}

export interface MonitorOptions {
  app?: string | null;
  process?: string | null;
  mode?: string | null;
  transport?: Transport | null;
  cachelint?: boolean;
  vectors?: boolean | null;
}

interface CacheLintLike {
  preflight?(model: string, messages: unknown[]): string | null;
  verify?(key: string | null, usage: unknown): void;
}

export interface FinishArgs {
  usage: unknown;
  latencyMs: number | null;
  error: unknown;
}

/** Per-client monitor state: identity, mode sources, store and transport. */
export class Monitor {
  readonly provider: EventProvider;
  readonly app: string;
  readonly dir: string;
  private readonly transportRef: Transport;
  private readonly defaultProcess: string | null;
  private readonly modeArg: string | null;
  private readonly configDoc: CachedDoc<ConfigDoc & Record<string, unknown>>;
  private readonly remoteDoc: CachedDoc<Record<string, unknown> | null>;
  private readonly pinsDoc: CachedDoc<PinsDoc & Record<string, unknown>>;
  private readonly vectors: boolean;
  private hook: CacheLintLike | null = null;
  private readonly cachelint: boolean;
  private cacheEngine: Promise<CacheTracker | null> | null = null;
  private cacheTracker: CacheTracker | null = null;
  /** The Langfuse export is on for this process (SPEC.md §11). */
  private readonly langfuseExport: boolean;

  constructor(provider: EventProvider, opts: MonitorOptions = {}) {
    this.provider = provider;
    this.transportRef = opts.transport ?? getTransport();
    const env = envFromOs();
    let cwdName: string | null = null;
    try {
      cwdName = basename(process.cwd());
    } catch {
      cwdName = null;
    }
    this.app = resolveApp({ env, args: opts.app ?? null, cwdName }).app;
    this.defaultProcess = opts.process ?? null;
    this.modeArg = opts.mode ?? null;
    this.dir = store.appDir(this.transportRef.dataDir, this.app);
    this.configDoc = new CachedDoc(this.dir, (d) => store.readConfig(d) as ConfigDoc & Record<string, unknown>, store.configMtime);
    this.remoteDoc = new CachedDoc(this.dir, (d) => store.readRemote(d), store.remoteMtime, store.remoteGeneration);
    this.pinsDoc = new CachedDoc(this.dir, (d) => store.readPins(d) as PinsDoc & Record<string, unknown>, pinsMtime);
    try {
      this.transportRef.registerApp(this.dir);
    } catch (err) {
      debug('registerApp failed', err);
    }
    this.vectors = opts.vectors === null || opts.vectors === undefined ? vectorsEnabled() : Boolean(opts.vectors);
    if (opts.cachelint) this.hook = loadCacheLintHook();
    this.cachelint = Boolean(opts.cachelint);
    let exportOn = false;
    try {
      exportOn = langfuseSettings().enabled;
      if (exportOn) watchLangfuseSpans();
    } catch {
      exportOn = false;
    }
    this.langfuseExport = exportOn;
  }

  get transport(): Transport {
    return this.transportRef;
  }

  /** Wait for this monitor's pending background work (tests, shutdown). */
  drain(timeoutMs?: number): Promise<void> {
    return this.transportRef.drain(timeoutMs);
  }

  // -- 1. capture ------------------------------------------------------

  /** Identity and routing text for this call; null on any failure. */
  before(params: Record<string, unknown>, perCallProcess: unknown, stream: boolean): CallPlan | null {
    try {
      const site = captureCallSite();
      const callSite: CallSiteRecord | null = site
        ? { file: relativeFile(site.file) ?? site.file, line: site.line, function: site.function ?? null }
        : null;
      const proc = resolveProcess({
        perCall: perCallProcess,
        context: currentProcess(),
        default: this.defaultProcess,
        callSite,
      }).process;
      const resolved = resolveMode(proc, {
        env: envFromOs(),
        remote: this.remoteDoc.get(),
        args: this.modeArg,
        config: this.configDoc.get(),
      });
      const model = params.model;
      const modelRequested = typeof model === 'string' ? model : null;
      const registry = this.transportRef.registry();
      const anchor = modelRequested ? resolveAnchor(modelRequested, registry) : null;
      const [text, digest] = routingText(params.messages);
      const policy = this.transportRef.policyAllowed();
      const ids = registry.modelIds;
      return {
        ts: formatTs(clock.now()),
        app: this.app,
        process: proc,
        mode: resolved.mode,
        provider: this.provider,
        modelRequested,
        anchor,
        text,
        promptHash: digest,
        callSite,
        stream: Boolean(stream),
        target: null,
        modelSent: modelRequested,
        outcome: null,
        routedOutcome: null,
        managedModel: resolved.model,
        modeSource: resolved.source,
        statusOverride: null,
        policy,
        routeAllowed: routeAllowed(this.provider, ids, anchor, policy),
        shadowAllowed: shadowAllowed(ids, policy),
        lintKey: null,
        lintModel: null,
        lintMessages: null,
        lintTools: null,
        cacheTtlPolicy: cacheTtlPolicyFromRequest(params),
        otel: this.callOtel(),
      };
    } catch (err) {
      debug('before() failed', err);
      return null;
    }
  }

  /**
   * The event's `otel`: with the Langfuse export on, the generation Langfuse's
   * `observeOpenAI` started for this very call (see otel.ts); else the active
   * span; else null.
   */
  private callOtel(): OtelIds | null {
    if (this.langfuseExport) {
      watchLangfuseSpans(); // late OpenTelemetry set-up: from the next call on
      const generation = takeLangfuseSpan();
      if (generation !== null) return generation;
    }
    return activeSpanIds();
  }

  // -- 1b. first-call remote config (SPEC.md §3.3, TS-296) --------------

  /**
   * A promise to wait on before deciding this call, or null (the usual
   * case: no poller, the first answer already came, the first-call wait
   * ran out, or the env pins the mode). Cheap once settled.
   */
  remoteGate(plan: CallPlan): Promise<void> | null {
    if (plan.modeSource === 'env') return null;
    try {
      const t = this.transportRef as Transport & { remoteGate?: () => Promise<void> | null };
      return typeof t.remoteGate === 'function' ? t.remoteGate() : null;
    } catch {
      return null;
    }
  }

  /** Re-resolve the plan's mode after the first remote-config answer. Never throws. */
  settleMode(plan: CallPlan): void {
    try {
      const resolved = resolveMode(plan.process, {
        env: envFromOs(),
        remote: this.remoteDoc.get(),
        args: this.modeArg,
        config: this.configDoc.get(),
      });
      plan.mode = resolved.mode;
      plan.managedModel = resolved.model;
      plan.modeSource = resolved.source;
    } catch (err) {
      debug('settleMode() failed', err);
    }
  }

  // -- 2. decide -------------------------------------------------------

  needsDecision(plan: CallPlan): boolean {
    if (plan.mode === MODE_MANAGED) return true;
    return plan.mode !== MODE_OFF && Boolean(plan.text);
  }

  /**
   * Pick `plan.target` per SPEC.md §2.4-§2.5, §2.9. May route in-path
   * (dynamic, first stable call) with the reachable ∩ policy candidate list.
   */
  async decide(plan: CallPlan): Promise<void> {
    try {
      if (!this.needsDecision(plan)) return;
      if (plan.mode === MODE_DYNAMIC) {
        const winner = await this.routeInPath(plan);
        plan.target = routedModel(MODE_DYNAMIC, plan.provider, plan.anchor, winner, null, plan.policy);
      } else if (plan.mode === MODE_STABLE) {
        plan.target = await this.stableTarget(plan);
      } else if (plan.mode === MODE_MANAGED) {
        const decision = managedTarget(
          plan.provider,
          plan.anchor,
          plan.managedModel,
          this.transportRef.registry(),
          plan.policy,
        );
        plan.target = decision.target;
        plan.statusOverride = decision.route_status;
      }
    } catch (err) {
      debug('decide() failed', err);
      plan.target = null; // fail-open: the anchor is sent
    }
  }

  /**
   * Route this call in-path over the candidates it could actually be sent
   * to (reachable ∩ policy) and return that ranking's best-for-less model.
   * Skipped (null) when nothing but the anchor could be sent. The outcome
   * doubles as the shadow outcome when both lists are equal.
   */
  private async routeInPath(plan: CallPlan): Promise<string | null> {
    const candidates = plan.routeAllowed;
    if (candidates !== null && !candidates.some((m) => m !== plan.anchor)) return null;
    const o = await this.route(plan, true, candidates);
    plan.routedOutcome = o;
    if (sameList(candidates, plan.shadowAllowed)) plan.outcome = o;
    return this.winner(o, plan.anchor);
  }

  private async stableTarget(plan: CallPlan): Promise<string | null> {
    const pins = this.pinsDoc.get();
    const processes = isObject(pins.processes) ? pins.processes : {};
    const pin = processes[plan.process];
    if (pinValid(pin, plan.ts)) {
      return routedModel(MODE_STABLE, plan.provider, plan.anchor, null, pin.model, plan.policy);
    }
    const events = store.readEvents(this.dir, plan.ts, PIN_HISTORY_S);
    const decision = evaluatePin(
      events as Parameters<typeof evaluatePin>[0],
      pin,
      plan.ts,
      plan.process,
      this.modelFacts(),
    );
    let newPin: Pin;
    if (decision.action === 'pin') {
      newPin = decision.pin as Pin;
    } else {
      // No usable history: route this very call and pin its winner.
      const winner = await this.routeInPath(plan);
      if (!winner || !reachable(plan.provider, winner, plan.anchor)) return null;
      newPin = pinFromFirstCall(winner, plan.ts);
    }
    const doc = setPin(pins, plan.process, newPin);
    store.writePins(this.dir, doc);
    this.pinsDoc.set(doc as PinsDoc & Record<string, unknown>);
    return routedModel(MODE_STABLE, plan.provider, plan.anchor, null, newPin.model, plan.policy);
  }

  /** The catalog's benchmark score and price per model, the pin tie-break inputs (SPEC.md §2.5); null if unavailable. */
  private modelFacts(): ModelFactsMap | null {
    try {
      return modelFacts(this.transportRef.bundleInUse());
    } catch {
      return null;
    }
  }

  private route(
    plan: CallPlan,
    inPath: boolean,
    allowed: string[] | null,
    includeEmbedding: boolean | null = null,
  ): Promise<RouteOutcome> {
    const embed = includeEmbedding === null ? this.vectors : includeEmbedding;
    return this.transportRef.route(plan.text, { includeEmbedding: embed, inPath, allowedModels: allowed });
  }

  private winner(o: RouteOutcome | null, anchor: string | null): string | null {
    if (!outcomeOk(o)) return null;
    return pickWinner(o.scores, anchor, this.transportRef.registry()).winner;
  }

  // -- 3. apply to the request ----------------------------------------

  /** Rewrite `model` when a target was chosen; ask streams for usage. */
  apply(plan: CallPlan, params: Record<string, unknown>): void {
    try {
      if (plan.target) {
        plan.modelSent = nativeModelId(plan.provider, plan.target);
        params.model = plan.modelSent;
      }
      if (
        plan.stream &&
        (plan.provider === PROVIDER_OPENAI || plan.provider === PROVIDER_OPENROUTER) &&
        !('stream_options' in params)
      ) {
        params.stream_options = { include_usage: true };
      }
      if (this.hook !== null && typeof this.hook.preflight === 'function') {
        const messages = Array.isArray(params.messages) ? params.messages : [];
        plan.lintKey = this.hook.preflight(typeof params.model === 'string' ? params.model : '', messages);
      }
      if (this.cachelint && Array.isArray(params.messages)) {
        // Snapshot: the caller may push to its array before the event is
        // recorded. The engine loads in the background (multi-MB ranks).
        plan.lintModel = typeof params.model === 'string' ? params.model : '';
        plan.lintMessages = params.messages.map((m) => (isObject(m) ? { ...m } : m));
        const schemas = [
          ...(Array.isArray(params.tools) ? params.tools : []),
          ...(Array.isArray(params.functions) ? params.functions : []),
        ];
        plan.lintTools = schemas.length > 0 ? schemas : null;
        void this.loadCacheEngine();
      }
    } catch (err) {
      debug('apply() failed', err);
    }
  }

  // -- 4. record -------------------------------------------------------

  /**
   * The API shape(s) of this client's usage for `normalizeCacheUsage`
   * (SPEC.md §3.1b): `openai` for OpenAI and Azure, `openrouter` for
   * OpenRouter; any other host is read as `openai` and then as `anthropic`
   * (OpenAI-compatible proxies in front of Claude report
   * `cache_creation_input_tokens`).
   */
  usageShape(): string | readonly string[] {
    if (this.provider === PROVIDER_OPENROUTER) return 'openrouter';
    if (this.provider === PROVIDER_OPENAI) return 'openai';
    return ['openai', 'anthropic'];
  }

  /** Hand the finished call to the transport (never throws). */
  finish(plan: CallPlan, args: FinishArgs): void {
    try {
      if (this.hook !== null && plan.lintKey && typeof this.hook.verify === 'function' && isObject(args.usage)) {
        this.hook.verify(plan.lintKey, args.usage);
      }
      const usage = usageFromProvider(this.usageShape(), args.usage);
      const error = errorRecord(args.error);
      if (error !== null) {
        // A failed call is recorded right away and never routed: nothing to
        // learn from a prompt the provider rejected, and the record must
        // survive a process that exits on the error.
        this.write(plan, plan.outcome ?? makeOutcome(STATUS_SKIPPED), usage, args.latencyMs, error);
        return;
      }
      const job = async (): Promise<void> => {
        let o = plan.outcome;
        if (o === null) {
          if (plan.text) {
            // Ask for the embedding only if the in-path route did not
            // already bring one.
            const routed = plan.routedOutcome;
            const have = routed !== null && isObject(routed.embedding);
            o = await this.route(plan, false, plan.shadowAllowed, this.vectors && !have);
          } else {
            o = makeOutcome(STATUS_SKIPPED);
          }
        }
        if (plan.lintMessages) await this.loadCacheEngine();
        this.write(plan, o, usage, args.latencyMs, error);
      };
      if (!this.transportRef.submit(job)) {
        this.write(plan, plan.outcome ?? makeOutcome(STATUS_QUEUE_FULL), usage, args.latencyMs, error);
      }
    } catch (err) {
      debug('finish() failed', err);
    }
  }

  private write(plan: CallPlan, o: RouteOutcome, usage: Usage | null, latencyMs: number | null, error: ErrorRecord | null): void {
    const registry = this.transportRef.registry();
    let winner: string | null = null;
    let winnerReachable = false;
    let classification = null;
    if (outcomeOk(o)) {
      winner = pickWinner(o.scores, plan.anchor, registry, plan.policy).winner;
      winnerReachable = reachable(plan.provider, winner, plan.anchor);
      classification = classificationFromWire(o.classification);
    } else if (plan.routedOutcome !== null && outcomeOk(plan.routedOutcome)) {
      classification = classificationFromWire(plan.routedOutcome.classification);
    }
    const usedCatalog = plan.target ?? plan.anchor;
    let embedding: unknown = o.embedding;
    if (!isObject(embedding) && plan.routedOutcome !== null) embedding = plan.routedOutcome.embedding;
    let vectorWritten = false;
    if (this.vectors && plan.text && isObject(embedding)) {
      try {
        const emb = embedding;
        const record = buildVector({
          prompt_hash: plan.promptHash,
          ts: plan.ts,
          process: plan.process,
          model: String(emb.model ?? ''),
          vector: Array.isArray(emb.v) ? (emb.v as number[]) : [],
        });
        vectorWritten = store.appendVector(this.dir, record);
      } catch (err) {
        debug('vector write failed', err); // the event is still worth writing
      }
    }
    const event = buildEvent({
      ts: plan.ts,
      app: plan.app,
      process: plan.process,
      call_site: plan.callSite,
      provider: plan.provider,
      model_requested: plan.modelRequested,
      model_resolved: plan.anchor,
      model_used: plan.modelSent,
      mode: plan.mode,
      winner,
      winner_reachable: winnerReachable,
      route_status: plan.statusOverride || o.status,
      classification,
      usage,
      est_cost_usd: estimateCost(registry, usedCatalog, usage as Record<string, unknown> | null),
      est_cost_winner_usd: estimateCost(registry, winner, usage as Record<string, unknown> | null),
      est_cost_anchor_usd: estimateCost(registry, plan.anchor, usage as Record<string, unknown> | null),
      latency_ms: latencyMs,
      stream: plan.stream,
      error,
      prompt_hash: plan.promptHash,
      vector: vectorWritten,
      taxonomy_version: this.transportRef.taxonomyVersion(),
      cache_ttl_policy: plan.cacheTtlPolicy ?? null,
      cache: this.cacheBlock(plan),
      otel: plan.otel ?? null,
    });
    store.appendEvent(this.dir, event);
  }

  /** Load the cachelint engine once; resolves to null when it cannot load. */
  private loadCacheEngine(): Promise<CacheTracker | null> {
    this.cacheEngine ??= import('../cachelint/index.js').then(
      (engine) => (this.cacheTracker = new CacheTracker(engine)),
      (err) => {
        debug('cachelint engine failed to load', err);
        return null;
      },
    );
    return this.cacheEngine;
  }

  /**
   * `{outcome, report}` when this call was linted, else null. A call written
   * before the engine finished loading (an error recorded right away, a full
   * queue) has no block.
   */
  private cacheBlock(plan: CallPlan): CacheBlock | null {
    const messages = plan.lintMessages;
    if (!messages || this.cacheTracker === null) return null;
    const tools = plan.lintTools ?? undefined;
    plan.lintMessages = null;
    plan.lintTools = null;
    try {
      return this.cacheTracker.observe(plan.process, plan.lintModel ?? '', messages, tools);
    } catch (err) {
      debug('cache block failed', err); // fail-open: the event is still written
      return null;
    }
  }
}

function loadCacheLintHook(): CacheLintLike | null {
  // The Node cachelint hook's preflight() is async (it loads the tokenizer
  // ranks lazily), which does not fit the synchronous apply() step: the
  // stderr warnings and VERIFY_MISS stay Python-only for now. The event's
  // `cache` block (API.md v1.4) is computed off the hot path instead, by
  // Monitor.cacheBlock() once the engine has loaded.
  return null;
}

/** `{status, class}` for a provider error (never the message text). */
export function errorRecord(err: unknown): ErrorRecord | null {
  if (err === null || err === undefined) return null;
  const e = err as { status?: unknown; constructor?: { name?: string } };
  const status = typeof e.status === 'number' && Number.isInteger(e.status) ? e.status : null;
  const cls = (typeof err === 'object' && err !== null && err.constructor && err.constructor.name) || 'Error';
  return { status, class: String(cls) };
}

function elapsedMs(t0: number): number {
  return Math.trunc(performance.now() - t0);
}

// ---------------------------------------------------------------------------
// Stream wrapper
// ---------------------------------------------------------------------------

type Done = (usage: unknown, err: unknown) => void;

/**
 * Forwards every chunk and records the call once the stream ends
 * (exhausted, returned early, or failed). `usage` arrives on the last chunk
 * when `stream_options.include_usage` is on. `tee()` is not supported.
 */
export class MonitoredStream<T = unknown> implements AsyncIterable<T> {
  readonly controller: unknown;
  private usage: unknown = null;
  private finished = false;

  constructor(
    private readonly inner: AsyncIterable<T> & { controller?: unknown },
    private readonly done: Done,
  ) {
    this.controller = inner.controller;
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
      for await (const chunk of this.inner) {
        const usage = (chunk as { usage?: unknown } | null)?.usage;
        if (usage !== null && usage !== undefined) this.usage = usage;
        yield chunk;
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
// The create() wrapper
// ---------------------------------------------------------------------------

type CreateFn = (this: unknown, body: Record<string, unknown>, options?: unknown) => unknown;

interface WrappedCreate extends CreateFn {
  __wrapped__?: CreateFn;
  __tryaii_monitor__?: Monitor;
}

function runCall(
  monitor: Monitor,
  original: CreateFn,
  target: unknown,
  plan: CallPlan,
  params: Record<string, unknown>,
  options: unknown,
): unknown {
  monitor.apply(plan, params);
  const t0 = performance.now();
  let p: unknown;
  try {
    p = original.call(target, params, options);
  } catch (err) {
    monitor.finish(plan, { usage: null, latencyMs: elapsedMs(t0), error: err });
    throw err;
  }
  if (!p || typeof (p as Promise<unknown>).then !== 'function') {
    // Not a promise (a mocked client): record what we can and pass it through.
    monitor.finish(plan, { usage: (p as { usage?: unknown } | null)?.usage ?? null, latencyMs: elapsedMs(t0), error: null });
    return p;
  }
  const promise = p as Promise<unknown>;
  if (plan.stream) {
    return promise.then(
      (s) =>
        new MonitoredStream(s as AsyncIterable<unknown> & { controller?: unknown }, (usage, err) =>
          monitor.finish(plan, { usage, latencyMs: elapsedMs(t0), error: err }),
        ),
      (err) => {
        monitor.finish(plan, { usage: null, latencyMs: elapsedMs(t0), error: err });
        throw err;
      },
    );
  }
  // Side listeners only: the provider's own promise (APIPromise) is returned.
  promise.then(
    (resp) => monitor.finish(plan, { usage: (resp as { usage?: unknown } | null)?.usage ?? null, latencyMs: elapsedMs(t0), error: null }),
    (err) => monitor.finish(plan, { usage: null, latencyMs: elapsedMs(t0), error: err }),
  );
  return promise;
}

/** The `create` replacement for `monitor` around `original` (exported for tests). */
export function makeCreate(monitor: Monitor, original: CreateFn): WrappedCreate {
  // A plain function, so the call site and the process context are captured
  // synchronously, before the first await.
  const create = function (this: unknown, body: Record<string, unknown>, options?: unknown): unknown {
    const params: Record<string, unknown> = isObject(body) ? { ...body } : {};
    const perCall = params[PER_CALL_PROCESS_KEY];
    delete params[PER_CALL_PROCESS_KEY];
    const stream = Boolean(params.stream);
    const plan = monitor.before(params, perCall, stream);
    if (plan === null) return original.call(this, isObject(body) ? params : body, options);
    const gate = monitor.remoteGate(plan);
    if (gate !== null) {
      // The first call of the process: wait (bounded, once) for the first
      // remote-config answer, then decide with it (SPEC.md §3.3, TS-296).
      return gate.then(() => {
        monitor.settleMode(plan);
        if (monitor.needsDecision(plan)) {
          return monitor.decide(plan).then(() => runCall(monitor, original, this, plan, params, options));
        }
        return runCall(monitor, original, this, plan, params, options);
      });
    }
    if (monitor.needsDecision(plan)) {
      return monitor.decide(plan).then(() => runCall(monitor, original, this, plan, params, options));
    }
    return runCall(monitor, original, this, plan, params, options);
  } as WrappedCreate;
  create.__wrapped__ = original;
  create.__tryaii_monitor__ = monitor;
  return create;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export interface WrapOptions {
  app?: string | null;
  process?: string | null;
  mode?: string | null;
  routeFn?: RouteFn | null;
  cachelint?: boolean;
  inline?: boolean | null;
  vectors?: boolean | null;
  /**
   * Test seam with `routeFn` / `inline`: the transport's data-save policy
   * (`'auto'` = the saved one, the default; null = none; an array = the
   * allowed ids as is).
   */
  policy?: TransportPolicy;
}

interface Defaults {
  app: string | null;
  process: string | null;
  mode: string | null;
  cachelint: boolean;
}

export const defaults: Defaults = { app: null, process: null, mode: null, cachelint: false };

interface ClientLike {
  baseURL?: unknown;
  base_url?: unknown;
  chat: { completions: { create: CreateFn } & Record<string, unknown> };
}

function clientProvider(client: ClientLike): Provider {
  const baseUrl = client.baseURL ?? client.base_url;
  return providerClass(baseUrl === null || baseUrl === undefined ? null : String(baseUrl));
}

/** The own data property `key` of `obj`, read without running a getter or a Proxy `get` trap. */
function ownValue(obj: unknown, key: string): unknown {
  if (!isObject(obj)) return undefined;
  const d = Object.getOwnPropertyDescriptor(obj, key);
  return d && 'value' in d ? d.value : undefined;
}

/**
 * The real `chat.completions` object behind a Langfuse `observeOpenAI(client)`
 * proxy, or null for any other client.
 *
 * `observeOpenAI` (@langfuse/openai) returns a Proxy with only a `get` trap:
 * every read of a nested object returns a NEW proxy and every read of a
 * method returns a NEW tracing closure. Patching through it would hand
 * `makeCreate` a tracing closure as `original`, and Langfuse would wrap our
 * `create` again on the next read: two Langfuse generations per call.
 * Langfuse sets no marker, so the proxy is recognised by that shape: `chat`
 * read through the client differs from the client's own `chat` property and
 * changes on every read, and so does `completions.create`. The proxy has no
 * `getOwnPropertyDescriptor` trap, so the own properties are the target's.
 * A plain (or Azure, or any OpenAI-compatible) client returns its own
 * `chat` object and is rejected by the first check.
 */
function langfuseCompletions(c: ClientLike, chat: unknown): ClientLike['chat']['completions'] | null {
  const rawChat = ownValue(c, 'chat');
  if (rawChat === undefined || rawChat === chat || !isObject(rawChat)) return null;
  if (c.chat === chat) return null; // a stable wrapper object, not observeOpenAI
  const raw = ownValue(rawChat, 'completions');
  if (!isObject(raw)) return null;
  const proxied = (chat as ClientLike['chat']).completions;
  if (proxied === raw) return null;
  const first = proxied.create;
  if (typeof first !== 'function' || first === proxied.create || first === raw.create) return null;
  return raw as ClientLike['chat']['completions'];
}

/**
 * Monitor every `client.chat.completions.create` call of `client`.
 *
 * `client` is an `openai` `OpenAI` (or anything with `baseURL` and
 * `chat.completions.create`). `app` / `process` / `mode` set this client's
 * defaults (SPEC.md §2.6). `routeFn` and `inline` are the test seam: a
 * function replacing the daemon, run immediately. Returns the Monitor; the
 * client is patched in place.
 */
export function wrap(client: unknown, opts: WrapOptions = {}): Monitor {
  const c = client as ClientLike;
  const chat = c.chat;
  // A Langfuse `observeOpenAI` client: patch the real `completions` behind
  // the proxy so Langfuse traces each call once. Every other client takes
  // `chat.completions` exactly as before.
  const completions = langfuseCompletions(c, chat) ?? chat.completions;
  let original = completions.create as WrappedCreate;
  if (original.__wrapped__ && original.__tryaii_monitor__) {
    original = original.__wrapped__ as WrappedCreate; // re-wrapping replaces the earlier monitor
  }
  let transport: Transport | null = null;
  if (opts.routeFn || opts.inline) {
    transport = new Transport(null, {
      routeFn: opts.routeFn ?? null,
      inline: opts.inline === null || opts.inline === undefined ? true : opts.inline,
      policy: opts.policy,
    });
    transport.start();
  }
  const monitor = new Monitor(clientProvider(c), {
    app: opts.app ?? defaults.app,
    process: opts.process ?? defaults.process,
    mode: opts.mode ?? defaults.mode,
    transport,
    cachelint: Boolean(opts.cachelint) || defaults.cachelint,
    vectors: opts.vectors ?? null,
  });
  completions.create = makeCreate(monitor, original);
  return monitor;
}

export interface InstallOptions {
  app?: string | null;
  process?: string | null;
  mode?: string | null;
  cachelint?: boolean;
}

/**
 * Set the defaults every later `wrap()` call starts from. Unlike the Python
 * SDK, the Node wrapper does not patch the `openai` classes (ESM modules
 * are not patchable in place): call `wrap(client)` for each client.
 */
export function install(opts: InstallOptions = {}): void {
  defaults.app = opts.app ?? null;
  defaults.process = opts.process ?? null;
  defaults.mode = opts.mode ?? null;
  defaults.cachelint = Boolean(opts.cachelint);
}
