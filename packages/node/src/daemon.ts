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
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

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

/** A running daemon matching this runtime + embedding model, or null. */
export async function liveState(config: TryaiiDreConfig): Promise<DaemonState | null> {
  const state = readState(config);
  if (!state) return null;
  if (state.runtime !== RUNTIME) return null;
  if (state.embeddingModel !== config.embeddingModel) return null;
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

function spawnServe(config: TryaiiDreConfig): ChildProcess {
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
  let info = await liveState(config);
  if (info) return info;
  if (opts.autostart === false) return null;

  // The lock and log files live in the data dir, so it must exist before we
  // try to create them (first-ever run starts from nothing).
  mkdirSync(config.dataDir, { recursive: true });
  const deadline = Date.now() + (opts.waitTimeoutMs ?? waitSeconds() * 1000);
  const acquired = acquireSpawnLock(config);
  let child: ChildProcess | null = null;
  try {
    if (acquired) {
      clearState(config);
      child = spawnServe(config);
    }
    let notified = false;
    while (Date.now() < deadline) {
      info = await liveState(config);
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

export async function stop(config: TryaiiDreConfig): Promise<boolean> {
  const state = readState(config);
  if (!state) return false;
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
    topBenchmarks: (s.topBenchmarks ?? []).map(([name, value]) => [name, value] as [string, number]),
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
