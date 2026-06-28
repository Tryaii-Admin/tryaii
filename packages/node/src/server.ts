/**
 * Server side of the routing daemon (see docs/daemon.md).
 *
 * Builds a Router, warms the embedding model once, then serves routing
 * requests over a loopback socket until idle or shut down. Run as its own
 * process by the daemon auto-start (see daemon.ts spawnServe), never on the
 * client's hot path.
 */

import { randomBytes } from 'node:crypto';
import { readFileSync } from 'node:fs';
import * as net from 'node:net';
import { fileURLToPath } from 'node:url';

import type { ClassificationResult } from './classifiers/base.js';
import { createDefaultConfig, type TryaiiDreConfig } from './config.js';
import {
  clearState,
  idleSeconds,
  readState,
  RUNTIME,
  writeState,
  type DaemonState,
} from './daemon.js';
import { Router, type RouteOptions, type RouteResult } from './router.js';
import type { ModelScore } from './scoring/engine.js';
import { Priorities } from './scoring/priorities.js';

/** Minimal routing surface so tests can inject a fake router. */
export interface RouterLike {
  route(prompt: string, opts?: RouteOptions): Promise<RouteResult>;
}

export interface ServeOptions {
  idleTimeout?: number;
  router?: RouterLike;
  log?: (message: string) => void;
  onReady?: (state: DaemonState) => void;
}

// ---------------------------------------------------------------------------
// Serialization (mirrors daemon.deserializeRouteResult and the Python SDK)
// ---------------------------------------------------------------------------

function serializeScore(score: ModelScore): Record<string, unknown> {
  return {
    modelId: score.modelId,
    finalScore: score.finalScore,
    qualityScore: score.qualityScore,
    costScore: score.costScore,
    speedScore: score.speedScore,
    qualityContribution: score.qualityContribution,
    costContribution: score.costContribution,
    speedContribution: score.speedContribution,
    topBenchmarks: score.topBenchmarks.map(([name, value]) => [name, value]),
    reasoning: score.reasoning,
  };
}

function serializeClassification(
  classification: ClassificationResult | null,
): Record<string, unknown> | null {
  if (!classification) return null;
  return {
    benchmarkScores: { ...classification.benchmarkScores },
    broadCategory: classification.broadCategory,
    subcategory: classification.subcategory,
    confidence: classification.confidence,
    classifierUsed: classification.classifierUsed,
    cacheHit: classification.cacheHit,
    processingTimeMs: classification.processingTimeMs,
    difficulty: classification.difficulty ?? 0,
  };
}

export function serializeRouteResult(result: RouteResult): Record<string, unknown> {
  return {
    bestModel: result.bestModel,
    scores: result.scores.map(serializeScore),
    classification: serializeClassification(result.classification),
    priorities: result.priorities ? result.priorities.toDict() : null,
  };
}

function version(): string {
  try {
    // dist/server.js -> ../package.json resolves to the package root.
    return (
      (JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf-8')) as {
        version?: string;
      }).version ?? '0.0.0'
    );
  } catch {
    return '0.0.0';
  }
}

// ---------------------------------------------------------------------------
// Request handling
// ---------------------------------------------------------------------------

interface DaemonRequest {
  token?: string;
  cmd?: string;
  prompt?: string;
  priorities?: { quality?: number; cost?: number; speed?: number };
  topK?: number;
}

async function handle(
  req: DaemonRequest,
  router: RouterLike,
  token: string,
  state: DaemonState,
): Promise<Record<string, unknown>> {
  if (req.token !== token) return { ok: false, error: 'unauthorized' };

  switch (req.cmd) {
    case 'ping':
      return {
        ok: true,
        pong: true,
        runtime: state.runtime,
        version: state.version,
        embeddingModel: state.embeddingModel,
        pid: state.pid,
        uptimeMs: Date.now() - state.startedAtMs,
      };
    case 'shutdown':
      return { ok: true, bye: true };
    case 'route': {
      const prompt = req.prompt;
      if (typeof prompt !== 'string' || !prompt) {
        return { ok: false, error: 'prompt must be a non-empty string' };
      }
      const pr = req.priorities ?? {};
      const priorities = new Priorities(pr.quality ?? 3, pr.cost ?? 3, pr.speed ?? 3);
      const topK = Number(req.topK ?? 5);
      const result = await router.route(prompt, { priorities, topK });
      return { ok: true, result: serializeRouteResult(result) };
    }
    default:
      return { ok: false, error: `unknown command: ${req.cmd}` };
  }
}

// ---------------------------------------------------------------------------
// Serve loop
// ---------------------------------------------------------------------------

/** Run the routing daemon until idle or shut down. Resolves when stopped. */
export async function serve(
  config?: Partial<TryaiiDreConfig>,
  opts: ServeOptions = {},
): Promise<void> {
  const cfg = createDefaultConfig(config);
  const idle = opts.idleTimeout ?? idleSeconds();
  const log = opts.log ?? ((m: string) => process.stdout.write(m + '\n'));

  let router = opts.router;
  if (!router) {
    const real = new Router({ config: { embeddingModel: cfg.embeddingModel, dataDir: cfg.dataDir } });
    log(`[daemon] loading embedding model '${cfg.embeddingModel}' (one-time)...`);
    const started = Date.now();
    await real.route('warmup', { topK: 1 });
    log(`[daemon] model warm in ${((Date.now() - started) / 1000).toFixed(1)}s`);
    router = real;
  }

  await new Promise<void>((resolve) => {
    const srv = net.createServer();
    let token = '';
    let state: DaemonState;
    let idleTimer: NodeJS.Timeout | null = null;
    let stopped = false;

    const onSignal = (): void => shutdown();

    const shutdown = (): void => {
      if (stopped) return;
      stopped = true;
      if (idleTimer) clearTimeout(idleTimer);
      process.off('SIGTERM', onSignal);
      process.off('SIGINT', onSignal);
      const current = readState(cfg);
      if (current && current.port === state.port && current.token === token) {
        clearState(cfg);
      }
      srv.close(() => {
        log('[daemon] stopped');
        resolve();
      });
    };

    const resetIdle = (): void => {
      if (idleTimer) clearTimeout(idleTimer);
      if (idle && idle > 0) {
        idleTimer = setTimeout(() => {
          log('[daemon] idle timeout reached; shutting down');
          shutdown();
        }, idle * 1000);
        idleTimer.unref?.();
      }
    };

    srv.on('connection', (sock) => {
      resetIdle();
      let buf = '';
      sock.setTimeout(30_000, () => sock.destroy());
      sock.on('data', (chunk) => {
        buf += chunk.toString('utf-8');
        const nl = buf.indexOf('\n');
        if (nl === -1) return;
        const line = buf.slice(0, nl);
        buf = '';
        let req: DaemonRequest;
        try {
          req = JSON.parse(line) as DaemonRequest;
        } catch {
          sock.end(JSON.stringify({ ok: false, error: 'malformed request' }) + '\n');
          return;
        }
        void handle(req, router as RouterLike, token, state)
          .catch((err: unknown) => ({
            ok: false,
            error: err instanceof Error ? err.message : String(err),
          }))
          .then((resp) => {
            sock.end(JSON.stringify(resp) + '\n');
            if (req.cmd === 'shutdown' && (resp as { ok?: boolean }).ok) {
              log('[daemon] shutdown requested');
              shutdown();
            }
          });
      });
      sock.on('error', () => {
        /* client vanished; ignore */
      });
    });

    srv.on('error', (err) => {
      log(`[daemon] server error: ${err instanceof Error ? err.message : String(err)}`);
      resolve();
    });

    srv.listen(0, '127.0.0.1', () => {
      const addr = srv.address() as net.AddressInfo;
      token = randomBytes(16).toString('hex');
      state = {
        runtime: RUNTIME,
        version: version(),
        embeddingModel: cfg.embeddingModel,
        host: addr.address,
        port: addr.port,
        token,
        pid: process.pid,
        startedAtMs: Date.now(),
      };
      // Write the state file only now that the model is warm: its presence is
      // the readiness signal clients poll for.
      writeState(cfg, state);
      log(
        `[daemon] listening on ${addr.address}:${addr.port} (pid ${process.pid}); ` +
          `idle timeout ${idle}s. Stops when idle, on SIGTERM, or with TRYAII_NO_DAEMON=1`,
      );
      resetIdle();
      opts.onReady?.(state);
    });

    process.on('SIGTERM', onSignal);
    process.on('SIGINT', onSignal);
  });
}

// When launched as its own process by the daemon auto-start, run the serve
// loop. The embedding model and data dir arrive via env vars set by the
// spawning client (there is no public `serve` subcommand).
if (process.argv[1] && process.argv[1] === fileURLToPath(import.meta.url)) {
  const overrides: Partial<TryaiiDreConfig> = {};
  const model = process.env.TRYAII_DRE_EMBEDDING_MODEL;
  const dataDir = process.env.TRYAII_DRE_DATA_DIR;
  if (model) overrides.embeddingModel = model;
  if (dataDir) overrides.dataDir = dataDir;
  void serve(overrides).catch((err: unknown) => {
    process.stderr.write(
      `[daemon] fatal: ${err instanceof Error ? err.message : String(err)}\n`,
    );
    process.exitCode = 1;
  });
}
