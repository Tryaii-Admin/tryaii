/**
 * SDK warn hook: pre-flight cache lint + runtime cache verification.
 *
 * This is the SDK's seat for the ACTUAL rendered outgoing prompt — post
 * routing, post message-assembly — and for the response `usage` fields.
 * The future AST-introspection phase extends this class.
 *
 * Behavior (see docs/sdk/client/cache-lint.md):
 *   - warn-only and fail-open: no method here ever throws — a lint failure
 *     must never break or block the user's API call;
 *   - problems only: warnings fire for BELOW_THRESHOLD,
 *     EFFECTIVELY_UNCACHEABLE, and UNKNOWN_THRESHOLD-with-blockers verdicts;
 *   - once per prompt shape: each unique (model, canonical prompt) pair warns
 *     at most once per client instance (FIFO-capped memory, stale reset);
 *   - verification: from the SECOND call with the same shape onward, a shape
 *     the lint predicted cacheable that reports 0 cached tokens in `usage`
 *     triggers a single VERIFY_MISS warning for that shape.
 *
 * This module is deliberately LIGHT: it has no static value-imports from the
 * engine, so the integrations can import it statically without loading the
 * multi-MB js-tiktoken rank data. The engine loads via a cached dynamic
 * import on the first preflight() only (same discipline as the CLI's
 * `await import('./cachelint/index.js')`).
 *
 * Message formats are byte-identical to the Python SDK's hook (pinned by
 * mirrored unit tests in cachelint-hook.test.ts / test_cachelint_hook.py).
 */

import { createHash } from 'node:crypto';

type Engine = typeof import('./index.js');

const PREFIX = '[tryaii cachelint]';
const MAX_SHAPES = 1024; // FIFO cap on remembered prompt shapes
const STALE_AFTER_MS = 3600 * 1000; // idle window after which a shape's counter resets

const NOTE_LINE =
  `${PREFIX} note: reported once per unique prompt shape per client; ` +
  "set cacheLint: 'off' or TRYAII_CACHE_LINT=off to disable";

// Verdicts that warn on preflight. UNKNOWN_THRESHOLD additionally requires a
// blocking finding: bare UNKNOWN is a knowledge-base gap (e.g. a deepseek
// slug), not a problem in the user's prompt.
const WARN_VERDICTS = new Set(['BELOW_THRESHOLD', 'EFFECTIVELY_UNCACHEABLE', 'UNKNOWN_THRESHOLD']);

// Verdicts under which a cache read is genuinely expected on repeat calls.
// CACHEABLE_WITH_ACTION is excluded: this SDK never sends cache_control, so
// zero cached tokens is the CORRECT outcome there, not a mismatch.
const EXPECT_CACHE_VERDICTS = new Set(['CACHEABLE', 'CACHEABLE_PREFIX']);

interface Prediction {
  model: string;
  verdict: string;
  stableTokens: number;
  minTokens: number | null;
  expectCache: boolean;
  calls: number;
  verifyWarned: boolean;
  lastSeen: number;
}

export interface CacheLintHookOptions {
  /** Warning line sink; defaults to process.stderr. */
  sink?: (line: string) => void;
  /** Monotonic-ish clock in milliseconds; defaults to Date.now. */
  now?: () => number;
  /** Engine loader test seam; defaults to importing the cachelint module. */
  loadEngine?: () => Promise<Engine>;
}

/**
 * Resolve the cacheLint mode and build the hook (or null when off).
 * Precedence: explicit option value > TRYAII_CACHE_LINT env var > off.
 * Ambient/env garbage is treated as off — it must never throw.
 */
export function buildCacheLintHook(value?: string): CacheLintHook | null {
  const mode = value ?? process.env.TRYAII_CACHE_LINT ?? '';
  return mode.trim().toLowerCase() === 'warn' ? new CacheLintHook() : null;
}

/** Per-client-instance lint state. Public methods never throw. */
export class CacheLintHook {
  private readonly _sink: (line: string) => void;
  private readonly _now: () => number;
  private readonly _loadEngine: () => Promise<Engine>;
  private _engine: Promise<Engine> | null = null;
  private _engineBroken = false;
  private readonly _seen = new Map<string, Prediction>();
  private _noteShown = false;

  constructor(options?: CacheLintHookOptions) {
    this._sink = options?.sink ?? ((line: string) => process.stderr.write(line + '\n'));
    this._now = options?.now ?? (() => Date.now());
    this._loadEngine = options?.loadEngine ?? (() => import('./index.js'));
  }

  private _emit(line: string): void {
    try {
      this._sink(line);
    } catch {
      // a broken sink must not break the call
    }
  }

  private _key(openrouterModel: string, canonical: string): string {
    return createHash('sha256')
      .update(openrouterModel + '\x00' + canonical, 'utf-8')
      .digest('hex')
      .slice(0, 16);
  }

  /** Lint the outgoing request; resolves to the shape key (or null on failure). */
  async preflight(
    openrouterModel: string,
    messages: Array<{ role: string; content: string }>,
  ): Promise<string | null> {
    try {
      return await this._preflight(openrouterModel, messages);
    } catch {
      return null; // fail-open: never break the user's call
    }
  }

  private async _preflight(
    openrouterModel: string,
    messages: Array<{ role: string; content: string }>,
  ): Promise<string | null> {
    if (this._engineBroken) return null;
    this._engine ??= this._loadEngine();
    let engine: Engine;
    try {
      engine = await this._engine;
    } catch {
      this._engineBroken = true; // permanent no-op; never retry a broken load
      return null;
    }

    const [canonical] = engine.buildCanonical({ messages });
    const key = this._key(openrouterModel, canonical);
    const now = this._now();

    const seen = this._seen.get(key);
    if (seen !== undefined) {
      if (now - seen.lastSeen > STALE_AFTER_MS) seen.calls = 0;
      seen.calls++;
      seen.lastSeen = now;
      return key;
    }

    if (this._seen.size >= MAX_SHAPES) {
      const oldest = this._seen.keys().next().value as string | undefined;
      if (oldest !== undefined) this._seen.delete(oldest);
    }

    const result = engine.analyze({
      prompt: { messages },
      llm: { provider: 'openrouter', name: openrouterModel },
    }) as { items: Array<Record<string, any>> };
    const item = result.items[0];
    const verdict = item.verdict as { code: string; summary: string };
    const limitedBy = item.stable_prefix.limited_by as Record<string, any> | null;

    // Concurrent first calls with the same shape can interleave across the
    // awaits above — keep the earlier record if one landed meanwhile.
    const raced = this._seen.get(key);
    if (raced !== undefined) {
      raced.calls++;
      raced.lastSeen = now;
      return key;
    }

    this._seen.set(key, {
      model: openrouterModel,
      verdict: verdict.code,
      stableTokens: item.stable_prefix.tokens as number,
      minTokens: item.threshold.min_tokens as number | null,
      expectCache: EXPECT_CACHE_VERDICTS.has(verdict.code),
      calls: 1,
      verifyWarned: false,
      lastSeen: now,
    });

    const triggered =
      WARN_VERDICTS.has(verdict.code) &&
      (verdict.code !== 'UNKNOWN_THRESHOLD' || limitedBy !== null);
    if (triggered) {
      this._emit(`${PREFIX} ${verdict.code} for ${openrouterModel}: ${verdict.summary}`);
      if (limitedBy !== null) {
        this._emit(
          `${PREFIX}   first blocker: ${limitedBy.kind} at ` +
            `${limitedBy.section}+${limitedBy.section_offset} ` +
            `(${Math.floor(limitedBy.pct_into_prompt)}% in)`,
        );
      }
      if (!this._noteShown) {
        this._noteShown = true;
        this._emit(NOTE_LINE);
      }
    }
    return key;
  }

  /** Compare the preflight prediction against the response usage fields. */
  verify(key: string | null, usage: unknown): void {
    try {
      this._verify(key, usage);
    } catch {
      // fail-open: never break the user's call
    }
  }

  private _verify(key: string | null, usage: unknown): void {
    if (key === null || usage === null || usage === undefined) return;
    if (typeof usage !== 'object' || Array.isArray(usage)) return;
    const u = usage as Record<string, unknown>;
    const rec = this._seen.get(key);
    if (rec === undefined || rec.calls < 2 || !rec.expectCache || rec.verifyWarned) return;

    let cached: number | null = null;
    const details = u.prompt_tokens_details;
    if (
      details !== null &&
      typeof details === 'object' &&
      typeof (details as Record<string, unknown>).cached_tokens === 'number'
    ) {
      cached = (details as Record<string, unknown>).cached_tokens as number;
    } else if (typeof u.cached_tokens === 'number') {
      cached = u.cached_tokens;
    }

    const discount = typeof u.cache_discount === 'number' ? u.cache_discount : null;

    if (cached === null && discount === null) return; // unverifiable — stay silent
    if ((cached ?? 0) === 0 && (discount ?? 0) === 0) {
      rec.verifyWarned = true;
      this._emit(
        `${PREFIX} VERIFY_MISS for ${rec.model}: predicted ~${rec.stableTokens} tok ` +
          `cacheable prefix, but usage reports 0 cached tokens on repeat call ` +
          `#${rec.calls} of this prompt shape — a silent invalidator or missing ` +
          `provider support may be the cause`,
      );
    }
  }
}
