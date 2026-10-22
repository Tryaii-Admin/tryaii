/**
 * Routing transport for the monitor wrapper. Mirrors monitor/transport.py.
 *
 * TS-253: the warm-up first builds the in-process engine (`./engine.js`: the
 * int8 ONNX embedding model on onnxruntime-node, the same model file,
 * tokenizer and centroids as the Python SDK). With it, routes run in this
 * process and need no Python and no daemon. When onnxruntime-node or the
 * model file (`tryaii-minilm-int8`, or TRYAII_EMBEDDING_MODEL_PATH) is
 * missing, the daemon below is the fallback for one release; when neither
 * works the calls are recorded metadata-only (`unavailable`) and the reason
 * is reported at most once per machine per day (SPEC.md §3.1a).
 *
 * The daemon path (fallback), off the hot path:
 *
 * One Transport per process tracks un-awaited background jobs in a bounded
 * pending set (Node has no worker thread to spare; the jobs are I/O-bound).
 * Shadow routing and record writing run as those jobs; `dynamic` calls (and
 * a `stable` process without a usable pin) route in-path with a short budget,
 * and only through a daemon that is already known to be live - the wrapper
 * never waits for a spawn and never loads the embedding model in the
 * application's process.
 *
 * Ordering (TS-173): Python runs the warm-up as the first job on its single
 * worker, so every shadow route queued behind it sees the daemon state the
 * warm-up found. Node jobs run concurrently, so `start()` keeps one `warmUp`
 * promise and every background route awaits it before probing the daemon
 * state (fail-open: a failed warm-up leaves no state and the route records
 * `unavailable`, as before). In-path routes never wait for it: they route
 * only through a state that is already known to be live.
 *
 * Drain: `drain(timeoutMs)` always resolves by its deadline, even while a
 * slow warm-up (catalog check, daemon spawn up to TRYAII_DAEMON_WAIT) still
 * holds the jobs queued behind it. As in Python, those jobs are not
 * recorded at that point: they stay pending (and count against QUEUE_MAX, so
 * a call made while the set is full is recorded at once with `queue_full`)
 * and record their event (`ok`, or `unavailable` if the warm-up found no
 * daemon) when the warm-up settles, or are recorded as `unavailable` by `atExit()` if the process exits first -
 * Python's daemon worker drops its queue at interpreter exit the same way.
 *
 * Test seam: `new Transport(config, { routeFn, inline: true })` replaces the
 * daemon with `routeFn(text) -> [scores, classification, embedding | null]`
 * (sync or async) and runs every job immediately (still awaitable through
 * `drain()`).
 *
 * Slice 2: the data-save policy is resolved once per process and evaluated
 * once per bundle (`policyAllowed`, SPEC.md §2.8); `route()` takes the
 * request's candidate list (`allowedModels`), sends it to a daemon that
 * advertises the feature and always filters the returned ranking too. With
 * TRYAII_API_KEY set, `start()` also starts the uploader (`./upload.js`) and
 * the remote-config long poll (`./remote.js`) for every app directory
 * registered with `registerApp`; at exit the routing jobs drain first
 * (<= 2 s), then the uploader gets one last pass (<= 2 s).
 */

import { createDefaultConfig, type TryaiiDreConfig } from '../config.js';
import {
  DEFAULT_ONNX_MODEL,
  MODELS,
  ENV_MODEL_PATH,
  clearUnavailable,
  reportUnavailable,
} from '../embeddings/modelStore.js';
import { starterBundle, type CatalogBundle } from '../catalog/bundle.js';
import {
  FEATURE_ALLOWED_MODELS,
  FEATURE_ROUTE_EMBEDDING,
  catalogKey,
  deserializeRouteResult,
  ensureDaemon,
  isDisabled,
  liveState,
  request,
  supports,
  type DaemonEmbedding,
  type DaemonState,
} from '../daemon.js';
import { PolicyError, resolvePolicy, type ModelPolicy, type PolicyLike } from '../policy.js';
import { ModelRegistry } from '../registry/models.js';
import { DEFAULT_PRIORITIES } from '../scoring/priorities.js';
import { apiKey, apiUrl } from './api.js';
import { InProcessEngine } from './engine.js';
import { taxonomyVersion, type RouteStatus } from './events.js';
import { RemotePoller, remoteEnabled } from './remote.js';
import { dataDirFromEnv } from './store.js';
import { Uploader, uploadEnabled } from './upload.js';
import { filterRanking, policyAllowed, type ScoreLike } from './winner.js';

export const QUEUE_MAX = 256;
/** Every ranked model: the winner rule needs the anchor's row (SPEC.md §2.3). */
export const TOP_K = 1000;
export const ENV_ROUTE_TIMEOUT_MS = 'TRYAII_MONITOR_ROUTE_TIMEOUT_MS';
export const DEFAULT_ROUTE_TIMEOUT_MS = 1500;
export const SHADOW_TIMEOUT_MS = 2000;
export const STATE_RECHECK_MS = 5000;
export const DRAIN_TIMEOUT_MS = 2000;
/** How long `atExit()` gives the pending calls to record `unavailable` once routing is over. */
const FLUSH_TIMEOUT_MS = 500;

export const STATUS_OK: RouteStatus = 'ok';
export const STATUS_UNAVAILABLE: RouteStatus = 'unavailable';
export const STATUS_TIMEOUT: RouteStatus = 'timeout';
export const STATUS_DISABLED: RouteStatus = 'disabled';
export const STATUS_QUEUE_FULL: RouteStatus = 'queue_full';
export const STATUS_SKIPPED: RouteStatus = 'skipped';

/** What one routing attempt produced (`status` per SPEC.md §3.1). */
export interface RouteOutcome {
  status: RouteStatus;
  scores: ScoreLike[];
  classification: unknown;
  embedding: DaemonEmbedding | null;
}

export function outcome(
  status: RouteStatus,
  scores: ScoreLike[] = [],
  classification: unknown = null,
  embedding: DaemonEmbedding | null = null,
): RouteOutcome {
  return { status, scores, classification, embedding };
}

export function outcomeOk(o: RouteOutcome | null | undefined): o is RouteOutcome {
  return o !== null && o !== undefined && o.status === STATUS_OK;
}

export type RouteFnResult = [ScoreLike[] | null | undefined, unknown, DaemonEmbedding | null | undefined];
export type RouteFn = (text: string) => RouteFnResult | Promise<RouteFnResult>;

/**
 * The data-save policy of a transport: `'auto'` = the saved policy (loaded
 * once per process), null = no policy, a ModelPolicy / policy.json object =
 * that policy, an array of catalog ids = the allowed ids as is (test seam).
 */
export type TransportPolicy = PolicyLike | readonly string[];

export interface TransportOptions {
  routeFn?: RouteFn | null;
  inline?: boolean;
  policy?: TransportPolicy;
}

export interface RouteOptions {
  includeEmbedding: boolean;
  timeoutMs?: number | null;
  inPath?: boolean;
  /** The request's candidate list (null = every model), SPEC.md §2.8. */
  allowedModels?: readonly string[] | null;
}

/** A service the transport feeds with app directories. */
interface AppService {
  register(directory: string): void;
}

const BROKEN = Symbol('broken policy');

export function inPathTimeoutMs(): number {
  const raw = (process.env[ENV_ROUTE_TIMEOUT_MS] ?? '').trim();
  const parsed = raw ? Number(raw) : DEFAULT_ROUTE_TIMEOUT_MS;
  const ms = Number.isFinite(parsed) ? Math.trunc(parsed) : DEFAULT_ROUTE_TIMEOUT_MS;
  return Math.max(ms, 1);
}

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function debug(message: string, err?: unknown): void {
  if (process.env.TRYAII_VERBOSE) {
    process.stderr.write(`[tryaii monitor] ${message}${err ? `: ${String(err)}` : ''}\n`);
  }
}

export class Transport {
  readonly config: TryaiiDreConfig;
  private readonly routeFn: RouteFn | null;
  private readonly inline: boolean;
  private readonly pending = new Set<Promise<void>>();
  private bundle: CatalogBundle | null = null;
  private registryCache: ModelRegistry | null = null;
  private state: DaemonState | null = null;
  private stateCheckedAt = -Infinity;
  /** The in-process engine (TS-253), set by the warm-up when available. */
  private engine: InProcessEngine | null = null;
  private started = false;
  /**
   * The warm-up (catalog selection + daemon), created once by `start()`;
   * never rejects. Background routes await it; in-path routes do not.
   */
  private warmUp: Promise<void> | null = null;
  /** Set by `close()`: routing is over; pending background routes record `unavailable` at once (TS-224). */
  private closing = false;
  private resolveClosed: () => void = () => {};
  private readonly closed = new Promise<void>((resolve) => {
    this.resolveClosed = resolve;
  });
  private readonly policySpec: TransportPolicy;
  private policy: ModelPolicy | typeof BROKEN | undefined = undefined;
  private policyCache: { bundle: CatalogBundle | null; allowed: string[] | null } | null = null;
  /** The uploader / remote poller (null until started, or when off). */
  uploader: (AppService & { drain(timeoutMs?: number): Promise<void> }) | null = null;
  remote: AppService | null = null;
  private readonly appDirs: string[] = [];

  constructor(config?: TryaiiDreConfig | null, opts: TransportOptions = {}) {
    this.config = config ?? createDefaultConfig({ dataDir: dataDirFromEnv() });
    this.routeFn = opts.routeFn ?? null;
    this.inline = Boolean(opts.inline);
    this.policySpec = opts.policy === undefined ? 'auto' : opts.policy;
  }

  get dataDir(): string {
    return this.config.dataDir;
  }

  /**
   * The pricing registry: the starter catalog until warm-up selects the
   * user's catalog (full after login).
   */
  registry(): ModelRegistry {
    if (this.registryCache === null) {
      this.bundle = starterBundle();
      this.registryCache = ModelRegistry.fromBundle(this.bundle);
    }
    return this.registryCache;
  }

  private setBundle(bundle: CatalogBundle): void {
    this.registryCache = ModelRegistry.fromBundle(bundle);
    this.bundle = bundle;
  }

  /** The catalog bundle in use (starter until warm-up). */
  bundleInUse(): CatalogBundle | null {
    this.registry();
    return this.bundle;
  }

  /** The bundle's taxonomy version (SPEC.md §3.1); null today. */
  taxonomyVersion(): string | null {
    try {
      return taxonomyVersion(this.bundleInUse());
    } catch {
      return null;
    }
  }

  /**
   * The policy-allowed catalog ids for the current bundle, or null (no
   * policy). Evaluated once per bundle; a broken saved policy fails closed
   * (`[]`). Pure: no embedding model, no daemon.
   */
  policyAllowed(): string[] | null {
    const spec = this.policySpec;
    if (Array.isArray(spec)) return [...(spec as readonly string[])];
    if (this.policy === undefined) {
      try {
        this.policy = resolvePolicy(spec as PolicyLike);
      } catch (err) {
        if (!(err instanceof PolicyError)) throw err;
        debug('saved policy unreadable; allowing nothing', err);
        this.policy = BROKEN;
      }
    }
    const registry = this.registry();
    const bundle = this.bundle;
    const cache = this.policyCache;
    if (cache !== null && cache.bundle === bundle) return cache.allowed;
    const allowed =
      this.policy === BROKEN ? [] : policyAllowed(this.policy, bundle ? bundle.origins : null, registry.modelIds);
    this.policyCache = { bundle, allowed };
    return allowed;
  }

  // -- upload + remote config (API.md §3) ------------------------------

  /**
   * An app directory this process records to: uploaded and kept in sync
   * with the remote config when those services run.
   */
  registerApp(directory: string): void {
    if (!this.appDirs.includes(directory)) this.appDirs.push(directory);
    for (const service of [this.uploader, this.remote]) {
      if (service !== null) service.register(directory);
    }
  }

  /**
   * Start the uploader and the remote poller when TRYAII_API_KEY is set
   * (each unless disabled by its env switch). Never throws.
   */
  private startServices(): void {
    try {
      const key = apiKey();
      if (!key) return;
      const base = apiUrl();
      const uploader = uploadEnabled() ? new Uploader(key, base) : null;
      const remote = remoteEnabled() ? new RemotePoller(key, base) : null;
      this.uploader = uploader;
      this.remote = remote;
      for (const service of [uploader, remote]) {
        if (service === null) continue;
        for (const directory of this.appDirs) service.register(directory);
        service.start();
      }
    } catch (err) {
      debug('upload/remote services failed to start', err);
    }
  }

  // -- jobs ------------------------------------------------------------

  /** Start the transport (once) and queue the warm-up job. */
  start(): void {
    if (this.started) return;
    this.started = true;
    if (this.routeFn !== null || this.inline) return;
    process.once('beforeExit', () => {
      void this.atExit();
    });
    this.warmUp = this.runWarmUp().catch((err) => debug('warm-up failed', err));
    const warmUp = this.warmUp;
    this.submit(() => warmUp);
    this.startServices();
  }

  /**
   * Process exit: finish queued records (<= 2 s); whatever is still waiting
   * for the warm-up is recorded now as `unavailable` (TS-224); then one last
   * upload pass (<= 2 s).
   */
  async atExit(): Promise<void> {
    await this.drain(DRAIN_TIMEOUT_MS);
    this.close();
    await this.drain(FLUSH_TIMEOUT_MS);
    const uploader = this.uploader;
    if (uploader !== null) await uploader.drain(DRAIN_TIMEOUT_MS);
  }

  /**
   * Run `job` in the background; false when too many jobs are pending (the
   * caller then records the call immediately with `queue_full`).
   */
  submit(job: () => void | Promise<void>): boolean {
    if (!this.inline && this.pending.size >= QUEUE_MAX) return false;
    let p: Promise<void>;
    try {
      p = Promise.resolve(job());
    } catch (err) {
      debug('job failed', err);
      return true;
    }
    const tracked: Promise<void> = p
      .catch((err) => debug('job failed', err))
      .finally(() => {
        this.pending.delete(tracked);
      });
    this.pending.add(tracked);
    return true;
  }

  /** Stop waiting for the warm-up: pending background routes record `unavailable` at once. */
  close(): void {
    this.closing = true;
    this.resolveClosed();
  }

  /** Wait up to `timeoutMs` for the pending jobs (process exit, tests). */
  async drain(timeoutMs: number = DRAIN_TIMEOUT_MS): Promise<void> {
    const deadline = Date.now() + timeoutMs;
    while (this.pending.size > 0 && Date.now() < deadline) {
      const remaining = Math.max(deadline - Date.now(), 0);
      await Promise.race([
        Promise.allSettled(Array.from(this.pending)),
        new Promise<void>((resolve) => setTimeout(resolve, remaining)),
      ]);
    }
  }

  private async runWarmUp(): Promise<void> {
    let bundle: CatalogBundle | null = null;
    try {
      const { selectCatalog } = await import('../catalog/client.js');
      bundle = (await selectCatalog('auto')).bundle;
    } catch (err) {
      debug('catalog selection failed; using the starter catalog', err);
    }
    if (bundle === null) bundle = starterBundle();
    this.setBundle(bundle);
    if (isDisabled()) return;
    const built = await InProcessEngine.create(this.config, bundle, TOP_K);
    if (built.engine !== null) {
      this.engine = built.engine;
      clearUnavailable(this.dataDir);
      return;
    }
    debug(`in-process engine unavailable (${built.reason}); trying the daemon`);
    try {
      this.state = await ensureDaemon(this.config, { bundle });
    } catch (err) {
      debug('daemon unavailable', err);
      this.state = null;
    }
    this.stateCheckedAt = Date.now();
    if (this.state === null) this.reportNoEngine(built.reason ?? 'not found');
  }

  /** Neither the in-process engine nor a daemon: say so, at most once a day. */
  private reportNoEngine(reason: string): void {
    const model = DEFAULT_ONNX_MODEL;
    try {
      reportUnavailable(model, reason, {
        dataDir: this.dataDir,
        warn: (msg) => process.stderr.write(`${msg}\n`),
        debug: (msg) => debug(msg),
        message:
          `tryaii monitor: embedding model ${model} is not available (${reason}); recording ` +
          `metadata only and sending the model in your code. Fix: npm install onnxruntime-node ` +
          `${MODELS[model].distribution}, or set ${ENV_MODEL_PATH} to a copy of the model file. ` +
          'Shown at most once a day; see: tryaii monitor status',
      });
    } catch (err) {
      debug('could not record the embedding model state', err);
    }
  }

  // -- routing ---------------------------------------------------------

  /**
   * The live daemon's state. `probe` (background jobs only, after the
   * warm-up settled) re-discovers a daemon at most every 5 s; the hot path
   * never waits for one.
   */
  private async daemonState(probe: boolean): Promise<DaemonState | null> {
    if (this.state !== null) return this.state;
    if (!probe) return null;
    const now = Date.now();
    if (now - this.stateCheckedAt < STATE_RECHECK_MS) return null;
    this.stateCheckedAt = now;
    try {
      const key = this.bundle !== null ? catalogKey(this.bundle) : undefined;
      this.state = await liveState(this.config, key);
    } catch {
      this.state = null;
    }
    return this.state;
  }

  /**
   * Route `text` and return the outcome; never rejects. `allowedModels`
   * (null = every model) limits the ranking: sent as the daemon's
   * `allowedModels` when it advertises the feature, and ALWAYS applied to
   * the returned ranking too (older daemons and the injected `routeFn`
   * return everything).
   */
  async route(text: string, opts: RouteOptions): Promise<RouteOutcome> {
    const result = await this.routeRaw(text, opts);
    const allowed = opts.allowedModels;
    if (allowed !== null && allowed !== undefined && outcomeOk(result)) {
      result.scores = filterRanking(result.scores, allowed);
    }
    return result;
  }

  private async routeRaw(text: string, opts: RouteOptions): Promise<RouteOutcome> {
    if (!text) return outcome(STATUS_SKIPPED);
    if (this.routeFn !== null) return this.routeInjected(text);
    if (isDisabled()) return outcome(STATUS_DISABLED);
    // A background route waits for the warm-up (the first job on Python's
    // worker); an in-path route never does.
    if (!opts.inPath && this.warmUp !== null) await Promise.race([this.warmUp, this.closed]);
    if (this.closing) return outcome(STATUS_UNAVAILABLE);
    if (this.engine !== null) return this.routeInProcess(this.engine, text, opts);
    const state = await this.daemonState(!opts.inPath);
    if (state === null) return outcome(STATUS_UNAVAILABLE);
    const payload: Record<string, unknown> = {
      cmd: 'route',
      prompt: text,
      priorities: DEFAULT_PRIORITIES.toDict(),
      topK: TOP_K,
    };
    // TS-170: the policy (and, in-path, reachability) filter the ranking in
    // the daemon; route() re-applies the list client-side as well.
    if (opts.allowedModels !== null && opts.allowedModels !== undefined && supports(state, FEATURE_ALLOWED_MODELS)) {
      payload.allowedModels = [...opts.allowedModels];
    }
    if (opts.includeEmbedding && supports(state, FEATURE_ROUTE_EMBEDDING)) {
      payload.includeEmbedding = true;
    }
    const budget =
      opts.timeoutMs !== null && opts.timeoutMs !== undefined
        ? opts.timeoutMs
        : opts.inPath
          ? inPathTimeoutMs()
          : SHADOW_TIMEOUT_MS;
    let resp: unknown;
    try {
      resp = await request(state, payload, budget);
    } catch (err) {
      // Gone, restarted or wedged: forget the state so the next hot-path call
      // does not wait the whole budget again; a background job re-probes within 5 s.
      this.state = null;
      if (err instanceof Error && /timed out/.test(err.message)) return outcome(STATUS_TIMEOUT);
      return outcome(STATUS_UNAVAILABLE);
    }
    if (!isObject(resp) || !resp.ok) return outcome(STATUS_UNAVAILABLE);
    const data = resp.result;
    let scores: ScoreLike[];
    let classification: unknown;
    try {
      const result = deserializeRouteResult(data as Parameters<typeof deserializeRouteResult>[0]);
      scores = result.scores as unknown as ScoreLike[];
      classification = result.classification;
    } catch {
      return outcome(STATUS_UNAVAILABLE);
    }
    const embedding = isObject(data) ? data.embedding : null;
    return outcome(STATUS_OK, scores, classification, isObject(embedding) ? (embedding as unknown as DaemonEmbedding) : null);
  }

  /** TS-253: route in this process, with the daemon's wire shape. */
  private async routeInProcess(engine: InProcessEngine, text: string, opts: RouteOptions): Promise<RouteOutcome> {
    let data: Record<string, unknown>;
    try {
      ({ data } = await engine.route(text, opts.includeEmbedding, opts.allowedModels ?? null));
    } catch (err) {
      debug('in-process route failed', err);
      return outcome(STATUS_UNAVAILABLE);
    }
    try {
      const result = deserializeRouteResult(data as Parameters<typeof deserializeRouteResult>[0]);
      const embedding = data.embedding;
      return outcome(
        STATUS_OK,
        result.scores as unknown as ScoreLike[],
        result.classification,
        isObject(embedding) ? (embedding as unknown as DaemonEmbedding) : null,
      );
    } catch {
      return outcome(STATUS_UNAVAILABLE);
    }
  }

  /** Whether routes run in this process (TS-253). */
  get inProcess(): boolean {
    return this.engine !== null;
  }

  private async routeInjected(text: string): Promise<RouteOutcome> {
    let result: RouteFnResult;
    try {
      result = await (this.routeFn as RouteFn)(text);
    } catch (err) {
      if (err instanceof Error && err.name === 'TimeoutError') return outcome(STATUS_TIMEOUT);
      return outcome(STATUS_UNAVAILABLE);
    }
    const [scores, classification, embedding] = result;
    return outcome(STATUS_OK, Array.from(scores ?? []), classification, isObject(embedding) ? (embedding as unknown as DaemonEmbedding) : null);
  }
}

let defaultTransport: Transport | null = null;

/** The process-wide transport (created and started on first use). */
export function getTransport(): Transport {
  if (defaultTransport === null) defaultTransport = new Transport();
  defaultTransport.start();
  return defaultTransport;
}
