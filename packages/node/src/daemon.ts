/**
 * Client side of the routing daemon (see docs/daemon.md).
 *
 * `tryaii route`/`tryaii eval` import this module to find -- and, when needed,
 * auto-start -- a long-lived background process that keeps the embedding model
 * warm. The heavy server lives in ./server.ts; this module stays lightweight.
 *
 * The protocol and state-file format are shared byte-for-byte with the Python
 * SDK (packages/python/tryaii/daemon.py); keep the two in sync.
 */

import { spawn, type ChildProcess } from 'node:child_process';
import {
  mkdirSync,
  readFileSync,
  renameSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import * as net from 'node:net';
import { join, relative, resolve as resolvePath, isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';

import { CatalogBundle, loadBundle, starterBundle } from './catalog/bundle.js';
import { fullDir } from './catalog/client.js';
import type { TryaiiDreConfig } from './config.js';
import type { ClassificationResult } from './classifiers/base.js';
import type { RouteResult } from './router.js';
import type { ModelScore } from './scoring/engine.js';
import { Priorities } from './scoring/priorities.js';

/** Identifies which SDK started a daemon; a client only reuses its own runtime. */
export const RUNTIME = 'node';
export const PROTOCOL_VERSION = 1;

const DEFAULT_IDLE_SECONDS = 900;
const DEFAULT_WAIT_SECONDS = 180;
const SPAWN_LOCK_STALE_MS = 300_000;

/** Daemon discovery/handshake record persisted to the state file. */
export interface DaemonState {
  runtime: string;
  version: string;
  embeddingModel: string;
  /** "<kind>:<version>" of the catalog the daemon routes on. */
  catalog?: string;
  host: string;
  port: number;
  token: string;
  pid: number;
  startedAtMs: number;
}

interface EnsureOptions {
  autostart?: boolean;
  waitTimeoutMs?: number;
  onStarting?: () => void;
  /**
   * The catalog the daemon must route on (default: starter). A daemon
   * running on another catalog kind/version is stopped and a new one started.
   */
  bundle?: CatalogBundle | null;
}

// ---------------------------------------------------------------------------
// Catalog (docs/catalog/CONTRACT-catalog-v1.md section 5)
// ---------------------------------------------------------------------------
// The daemon routes on exactly the catalog the CLI selected: the client passes
// it in TRYAII_DAEMON_CATALOG ("starter" or the absolute directory of a cached
// full-catalog version) and the daemon records "<kind>:<version>" in its state
// file. A daemon on another catalog (or a pre-catalog daemon with no
// "catalog" key) is stopped and replaced. Same as the Python SDK.

export const CATALOG_ENV = 'TRYAII_DAEMON_CATALOG';

/** "<kind>:<version>" of a CatalogBundle. */
export function catalogKey(bundle: CatalogBundle): string {
  return `${bundle.kind}:${bundle.version}`;
}

/**
 * What to hand the daemon for `bundle`: "starter", the full bundle's
 * directory, or null when it cannot be reloaded from disk.
 */
export function catalogSpec(bundle: CatalogBundle | null | undefined): string | null {
  if (!bundle || bundle.kind === 'starter') return 'starter';
  return bundle.directory ?? null;
}

/** Inverse of catalogSpec (server side). Empty = starter. */
export function bundleFromSpec(spec: string | null | undefined): CatalogBundle {
  if (!spec || spec === 'starter') return starterBundle();
  // A directory of the full-catalog cache is a cache load: its signature is
  // re-verified (catalog contract section 6). Other paths are the caller's
  // own bundles (new Router({ bundle })), loaded as given.
  const rel = relative(resolvePath(fullDir()), resolvePath(spec));
  const inCache = rel !== '' && !rel.startsWith('..') && !isAbsolute(rel);
  return loadBundle(spec, { verifySignature: inCache });
}

// ---------------------------------------------------------------------------
// Environment knobs
// ---------------------------------------------------------------------------

function envTruthy(name: string): boolean {
  return ['1', 'true', 'yes', 'on'].includes((process.env[name] ?? '').trim().toLowerCase());
}

export function isDisabled(): boolean {
  return envTruthy('TRYAII_NO_DAEMON');
}

function envInt(name: string, fallback: number): number {
  const raw = process.env[name];
  if (raw === undefined) return fallback;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? Math.trunc(parsed) : fallback;
}

export function idleSeconds(): number {
  return envInt('TRYAII_DAEMON_IDLE', DEFAULT_IDLE_SECONDS);
}

export function waitSeconds(): number {
  return envInt('TRYAII_DAEMON_WAIT', DEFAULT_WAIT_SECONDS);
}

// ---------------------------------------------------------------------------
// State file
// ---------------------------------------------------------------------------

export function statePath(config: TryaiiDreConfig): string {
  return join(config.dataDir, `daemon-${RUNTIME}.json`);
}

function lockPath(config: TryaiiDreConfig): string {
  return join(config.dataDir, `daemon-${RUNTIME}.lock`);
}

export function logPath(config: TryaiiDreConfig): string {
  return join(config.dataDir, `daemon-${RUNTIME}.log`);
}

export function readState(config: TryaiiDreConfig): DaemonState | null {
  try {
    return JSON.parse(readFileSync(statePath(config), 'utf-8')) as DaemonState;
  } catch {
    return null;
  }
}

export function writeState(config: TryaiiDreConfig, state: DaemonState): void {
  mkdirSync(config.dataDir, { recursive: true });
  const path = statePath(config);
  const tmp = `${path}.tmp`;
  writeFileSync(tmp, JSON.stringify(state), { encoding: 'utf-8', mode: 0o600 });
  renameSync(tmp, path);
}

export function clearState(config: TryaiiDreConfig): void {
  try {
    unlinkSync(statePath(config));
  } catch {
    /* already gone */
  }
}

// ---------------------------------------------------------------------------
// Socket request / response
// ---------------------------------------------------------------------------

interface DaemonResponse {
  ok: boolean;
  error?: string;
  [key: string]: unknown;
}

export function request(
  state: DaemonState,
  payload: Record<string, unknown>,
  timeoutMs: number,
): Promise<DaemonResponse> {
  return new Promise((resolve, reject) => {
    const sock = net.connect({ host: state.host, port: state.port });
    let buf = '';
    let settled = false;
    const finish = (err: Error | null, value?: DaemonResponse): void => {
      if (settled) return;
      settled = true;
      sock.destroy();
      if (err) reject(err);
      else resolve(value as DaemonResponse);
    };
    sock.setTimeout(timeoutMs, () => finish(new Error('daemon request timed out')));
    sock.on('connect', () => {
      sock.write(JSON.stringify({ ...payload, v: PROTOCOL_VERSION, token: state.token }) + '\n');
    });
    sock.on('data', (chunk) => {
      buf += chunk.toString('utf-8');
      const nl = buf.indexOf('\n');
      if (nl !== -1) {
        try {
          finish(null, JSON.parse(buf.slice(0, nl)) as DaemonResponse);
        } catch (err) {
          finish(err as Error);
        }
      }
    });
    sock.on('error', (err) => finish(err));
    sock.on('close', () => finish(new Error('daemon closed the connection without responding')));
  });
}

const delay = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

// ---------------------------------------------------------------------------
// Discovery
// ---------------------------------------------------------------------------

/**
 * A running daemon matching this runtime + embedding model (+ catalog key,
 * when given), or null.
 */
export async function liveState(
  config: TryaiiDreConfig,
  catalog?: string,
): Promise<DaemonState | null> {
  const state = readState(config);
  if (!state) return null;
  if (state.runtime !== RUNTIME) return null;
  if (state.embeddingModel !== config.embeddingModel) return null;
  if (catalog !== undefined && state.catalog !== catalog) return null;
  try {
    const resp = await request(state, { cmd: 'ping' }, 5000);
    return resp.ok ? state : null;
  } catch {
    return null;
  }
}

export async function status(config: TryaiiDreConfig): Promise<DaemonResponse | null> {
  const state = readState(config);
  if (!state) return null;
  try {
    const resp = await request(state, { cmd: 'ping' }, 5000);
    if (!resp.ok) return null;
    return { ...resp, host: state.host, port: state.port };
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Spawning
// ---------------------------------------------------------------------------

function acquireSpawnLock(config: TryaiiDreConfig): boolean {
  const path = lockPath(config);
  try {
    writeFileSync(path, String(process.pid), { flag: 'wx' });
    return true;
  } catch {
    try {
      if (Date.now() - statSync(path).mtimeMs > SPAWN_LOCK_STALE_MS) {
        unlinkSync(path);
        return acquireSpawnLock(config);
      }
    } catch {
      /* ignore */
    }
    return false;
  }
}

function releaseSpawnLock(config: TryaiiDreConfig): void {
  try {
    unlinkSync(lockPath(config));
  } catch {
    /* already gone */
  }
}

function spawnServe(config: TryaiiDreConfig, catalogSpecValue = 'starter'): ChildProcess {
  mkdirSync(config.dataDir, { recursive: true });
  // Launch the server module directly as a detached process -- there is no
  // public `serve` subcommand. server.js reads its model + data dir from the
  // env we hand it below, and runs the serve loop when invoked as the entry.
  const serverEntry = fileURLToPath(new URL('./server.js', import.meta.url));
  const child = spawn(
    process.execPath,
    [serverEntry],
    {
      detached: true,
      stdio: 'ignore',
      env: {
        ...process.env,
        TRYAII_DRE_EMBEDDING_MODEL: config.embeddingModel,
        TRYAII_DRE_DATA_DIR: config.dataDir,
        [CATALOG_ENV]: catalogSpecValue,
        TRYAII_NO_DAEMON: '1',
      },
    },
  );
  child.unref();
  return child;
}

/**
 * Return the state of a live daemon, starting one if needed.
 *
 * Returns null if no daemon could be reached (caller should fall back to
 * in-process routing).
 */
export async function ensureDaemon(
  config: TryaiiDreConfig,
  opts: EnsureOptions = {},
): Promise<DaemonState | null> {
  const bundle = opts.bundle ?? starterBundle();
  const key = catalogKey(bundle);
  const spec = catalogSpec(bundle);
  if (spec === null) return null; // an in-memory-only bundle cannot be handed to a daemon
  let info = await liveState(config, key);
  if (info) return info;
  if (opts.autostart === false) return null;
  const other = await liveState(config);
  if (other !== null && other.catalog !== key) {
    // Alive, same runtime + model, but another catalog: replace it -- unless
    // a concurrent CLI already replaced it with one on our catalog (stop()
    // rereads the state file and keeps a daemon on `key`).
    await stop(config, { keepCatalog: key });
    info = await liveState(config, key);
    if (info) return info;
  }

  // The lock and log files live in the data dir, so it must exist before we
  // try to create them (first-ever run starts from nothing).
  mkdirSync(config.dataDir, { recursive: true });
  const deadline = Date.now() + (opts.waitTimeoutMs ?? waitSeconds() * 1000);
  const acquired = acquireSpawnLock(config);
  let child: ChildProcess | null = null;
  try {
    if (acquired) {
      // A concurrent CLI may have started the right daemon (and released the
      // lock) since we looked: never clobber a live one on `key`.
      info = await liveState(config, key);
      if (info) return info;
      clearState(config);
      child = spawnServe(config, spec);
    }
    let notified = false;
    while (Date.now() < deadline) {
      info = await liveState(config, key);
      if (info) return info;
      if (child && child.exitCode !== null) return null;
      if (opts.onStarting && !notified) {
        opts.onStarting();
        notified = true;
      }
      await delay(250);
    }
    return null;
  } finally {
    if (acquired) releaseSpawnLock(config);
  }
}

/**
 * Stop the daemon. Resolves true if one was running.
 *
 * `keepCatalog`: a catalog key (`<kind>:<version>`); when the state file --
 * reread here -- already names that catalog, the daemon is left running (it
 * was started by a concurrent CLI on the catalog the caller wants) and false
 * is returned.
 */
export async function stop(
  config: TryaiiDreConfig,
  opts: { keepCatalog?: string } = {},
): Promise<boolean> {
  const state = readState(config);
  if (!state) return false;
  if (opts.keepCatalog !== undefined && state.catalog === opts.keepCatalog) return false;
  let stopped = false;
  try {
    const resp = await request(state, { cmd: 'shutdown' }, 5000);
    stopped = Boolean(resp.ok);
  } catch {
    /* fall through to signal */
  }
  if (!stopped && state.pid) {
    try {
      process.kill(state.pid, 'SIGTERM');
      stopped = true;
    } catch {
      /* process already gone */
    }
  }
  clearState(config);
  return stopped;
}

// ---------------------------------------------------------------------------
// Routing through the daemon
// ---------------------------------------------------------------------------

interface SerializedScore {
  modelId: string;
  finalScore: number;
  qualityScore: number;
  costScore: number;
  speedScore: number;
  qualityContribution: number;
  costContribution: number;
  speedContribution: number;
  // satisficing-v1 additions. Optional so a score serialized by an older
  // daemon still deserializes (the defaults below describe "no band known").
  bandBase?: number;
  inBand?: boolean;
  qualityTolerance?: number;
  qualityBest?: number;
  signalFlags?: string[];
  qPrime?: number;
  uCost?: number;
  uSpeed?: number;
  t300?: number | null;
  topBenchmarks: Array<[string, number]>;
  reasoning: string;
}

interface SerializedResult {
  bestModel?: string;
  scores?: SerializedScore[];
  classification?: (Omit<ClassificationResult, 'benchmarkScores'> & {
    benchmarkScores: Record<string, number>;
  }) | null;
  priorities?: { quality?: number; cost?: number; speed?: number } | null;
}

export function deserializeRouteResult(data: SerializedResult): RouteResult {
  const scores: ModelScore[] = (data.scores ?? []).map((s) => ({
    modelId: s.modelId,
    finalScore: s.finalScore,
    qualityScore: s.qualityScore,
    costScore: s.costScore,
    speedScore: s.speedScore,
    qualityContribution: s.qualityContribution,
    costContribution: s.costContribution,
    speedContribution: s.speedContribution,
    bandBase: s.bandBase ?? 0,
    inBand: s.inBand ?? false,
    qualityTolerance: s.qualityTolerance ?? 0,
    qualityBest: s.qualityBest ?? s.qualityScore,
    signalFlags: s.signalFlags ?? [],
    qPrime: s.qPrime ?? s.qualityScore,
    uCost: s.uCost ?? s.costScore,
    uSpeed: s.uSpeed ?? s.speedScore,
    t300: s.t300 ?? null,
    topBenchmarks: (s.topBenchmarks ?? []).map(([name, value]) => [name, value] as [string, number]),
    // Diagnostic only, deliberately not on the daemon wire -- a deserialized
    // score reports no per-term weights rather than inventing them.
    benchmarkWeights: {},
    reasoning: s.reasoning,
  }));

  const cls = data.classification;
  const classification: ClassificationResult | null = cls
    ? {
        benchmarkScores: { ...cls.benchmarkScores },
        broadCategory: cls.broadCategory,
        subcategory: cls.subcategory,
        confidence: cls.confidence,
        classifierUsed: cls.classifierUsed,
        cacheHit: cls.cacheHit,
        processingTimeMs: cls.processingTimeMs,
        difficulty: cls.difficulty,
      }
    : null;

  const pr = data.priorities ?? {};
  const priorities = new Priorities(pr.quality ?? 3, pr.cost ?? 3, pr.speed ?? 3);

  return { bestModel: data.bestModel ?? '', scores, classification, priorities };
}

/** Route a single prompt through a running daemon. Rejects on failure. */
export async function routeViaDaemon(
  state: DaemonState,
  prompt: string,
  priorities: Priorities,
  topK: number,
): Promise<RouteResult> {
  const resp = await request(
    state,
    { cmd: 'route', prompt, priorities: priorities.toDict(), topK },
    30_000,
  );
  if (!resp.ok) throw new Error(resp.error ?? 'daemon route failed');
  return deserializeRouteResult(resp.result as SerializedResult);
}

/**
 * A routing function that goes through the daemon at `state` and, the first
 * time a daemon request fails (it died mid-request, was replaced, answered an
 * error), switches to the in-process route function `fallback()` builds --
 * for that call and every later one.
 */
export function routeWithFallback(
  state: DaemonState,
  fallback: () => (prompt: string, priorities: Priorities, topK: number) => Promise<RouteResult>,
): (prompt: string, priorities: Priorities, topK: number) => Promise<RouteResult> {
  let inProcess: ((prompt: string, priorities: Priorities, topK: number) => Promise<RouteResult>) | null =
    null;
  return async (prompt, priorities, topK) => {
    if (inProcess === null) {
      try {
        return await routeViaDaemon(state, prompt, priorities, topK);
      } catch {
        inProcess = fallback();
      }
    }
    return inProcess(prompt, priorities, topK);
  };
}
