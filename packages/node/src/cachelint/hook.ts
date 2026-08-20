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
import { basename } from 'node:path';
import { fileURLToPath } from 'node:url';

type Engine = typeof import('./index.js');
type Introspect = typeof import('./introspect.js');
type CallSite = import('./introspect.js').CallSite;
type OverlaySlot = import('./introspect.js').OverlaySlot;

const PREFIX = '[tryaii cachelint]';
const MAX_SHAPES = 1024; // FIFO cap on remembered prompt shapes
const STALE_AFTER_MS = 3600 * 1000; // idle window after which a shape's counter resets
const MAX_SITES = 512; // FIFO cap on the per-call-site emission/value stores

// The package root for the stack-walk frame filter: '..' from this module is
// src/ under vitest and dist/ when installed — both are exactly what must be
// skipped as SDK-internal.
const PKG_ROOT = fileURLToPath(new URL('..', import.meta.url))
  .replace(/\\/g, '/')
  .toLowerCase();

/** Slot expr for warning lines: whitespace collapsed, capped at 60 chars. */
function displayExpr(expr: string): string {
  const collapsed = expr.replace(/\s+/g, ' ').trim();
  return collapsed.length <= 60 ? collapsed : collapsed.slice(0, 57) + '...';
}

/**
 * Synchronous structured-CallSite capture (spike-verified): V8 zero-cost
 * async CallSites carry the awaiting user frame through internal awaits;
 * node: internal frames (timers, task queues) interleave and are skipped;
 * a first foreign frame inside another node_modules tree is a wrapper
 * library, not user code — dead-end.
 */
function captureCallSite(): CallSite | null {
  const saved = Error.prepareStackTrace;
  const savedLimit = Error.stackTraceLimit;
  let sites: NodeJS.CallSite[];
  try {
    Error.stackTraceLimit = 40;
    Error.prepareStackTrace = (_err, callSites) => callSites;
    sites = new Error().stack as unknown as NodeJS.CallSite[];
  } finally {
    Error.prepareStackTrace = saved;
    Error.stackTraceLimit = savedLimit;
  }
  if (!Array.isArray(sites)) return null;
  for (const site of sites) {
    let file = site.getFileName();
    if (typeof file === 'string' && file.startsWith('file://')) {
      try {
        file = fileURLToPath(file);
      } catch {
        continue;
      }
    }
    if (!file) continue; // anonymous / eval frames
    if (file.startsWith('node:')) continue; // runtime internals interleave
    const norm = file.replace(/\\/g, '/').toLowerCase();
    if (norm.startsWith(PKG_ROOT)) continue; // SDK-internal
    if (norm.includes('/node_modules/')) return null; // wrapper library: dead-end
    const line = site.getLineNumber();
    if (line === null || line === undefined) continue;
    const col = site.getColumnNumber();
    return { file, line, col: col ?? null };
  }
  return null;
}

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
  templateSlots: OverlaySlot[] | null;
}

export interface CacheLintHookOptions {
  /** Warning line sink; defaults to process.stderr. */
  sink?: (line: string) => void;
  /** Monotonic-ish clock in milliseconds; defaults to Date.now. */
  now?: () => number;
  /** Engine loader test seam; defaults to importing the cachelint module. */
  loadEngine?: () => Promise<Engine>;
  /** Introspection loader test seam; defaults to importing the AST module. */
  loadIntrospect?: () => Promise<Introspect>;
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
  private readonly _loadIntrospect: () => Promise<Introspect>;
  private _introspect: Promise<Introspect> | null = null;
  private _introspectBroken = false;
  // AST-introspection emission state (once per call-site+slot, FIFO-capped).
  private readonly _siteWarned = new Map<string, null>();
  private readonly _slotValues = new Map<string, string>();

  constructor(options?: CacheLintHookOptions) {
    this._sink = options?.sink ?? ((line: string) => process.stderr.write(line + '\n'));
    this._now = options?.now ?? (() => Date.now());
    this._loadEngine = options?.loadEngine ?? (() => import('./index.js'));
    this._loadIntrospect = options?.loadIntrospect ?? (() => import('./introspect.js'));
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

  /**
   * Lint the outgoing request; resolves to the shape key (or null on failure).
   * `callSite` is an injected test seam; when absent the hook captures the
   * stack SYNCHRONOUSLY at entry (before any await) for AST introspection.
   */
  async preflight(
    openrouterModel: string,
    messages: Array<{ role: string; content: string }>,
    callSite?: CallSite | null,
  ): Promise<string | null> {
    let site: CallSite | null = callSite ?? null;
    if (site === null && callSite === undefined) {
      try {
        site = captureCallSite();
      } catch {
        site = null;
      }
    }
    try {
      return await this._preflight(openrouterModel, messages, site);
    } catch {
      return null; // fail-open: never break the user's call
    }
  }

  private async _preflight(
    openrouterModel: string,
    messages: Array<{ role: string; content: string }>,
    site: CallSite | null,
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

    const [canonical, sections] = engine.buildCanonical({ messages });
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

    const prediction: Prediction = {
      model: openrouterModel,
      verdict: verdict.code,
      stableTokens: item.stable_prefix.tokens as number,
      minTokens: item.threshold.min_tokens as number | null,
      expectCache: EXPECT_CACHE_VERDICTS.has(verdict.code),
      calls: 1,
      verifyWarned: false,
      lastSeen: now,
      templateSlots: null,
    };
    this._seen.set(key, prediction);

    // AST template introspection (fail-open; new shapes only by construction).
    const insights = await this._templateStep(messages, canonical, sections, item, site);
    if (insights) {
      prediction.templateSlots = insights.flatMap(([, classified]) =>
        classified.map(([slot]) => slot));
    }

    const triggered =
      WARN_VERDICTS.has(verdict.code) &&
      (verdict.code !== 'UNKNOWN_THRESHOLD' || limitedBy !== null);
    let emitted = false;
    if (triggered) {
      emitted = true;
      this._emit(`${PREFIX} ${verdict.code} for ${openrouterModel}: ${verdict.summary}`);
      if (limitedBy !== null) {
        this._emit(
          `${PREFIX}   first blocker: ${limitedBy.kind} at ` +
            `${limitedBy.section}+${limitedBy.section_offset} ` +
            `(${Math.floor(limitedBy.pct_into_prompt)}% in)`,
        );
        if (insights) this._emitAttribution(insights, limitedBy);
      }
    } else if (insights) {
      // Only today-silent verdicts reach here (BELOW_THRESHOLD always
      // triggers above) — the standalone slot lines fill the blind spot.
      emitted = this._emitSlotWarnings(insights);
    }
    if (emitted && !this._noteShown) {
      this._noteShown = true;
      this._emit(NOTE_LINE);
    }
    return key;
  }

  // -- AST template introspection --------------------------------------

  private async _templateStep(
    messages: Array<{ role: string; content: string }>,
    canonical: string,
    sections: Array<{ name: string; start: number; end: number }>,
    item: Record<string, any>,
    site: CallSite | null,
  ): Promise<Array<[CallSite, Array<[OverlaySlot, boolean]>]> | null> {
    try {
      if (site === null || this._introspectBroken) return null;
      this._introspect ??= this._loadIntrospect();
      let introspect: Introspect;
      try {
        introspect = await this._introspect;
      } catch {
        this._introspectBroken = true;
        return null;
      }

      const maps = await introspect.analyzeCallSite(site, messages);
      if (!maps) return null;
      const limitedBy = item.stable_prefix.limited_by as Record<string, any> | null;
      const boundary = limitedBy !== null ? (limitedBy.start as number) : canonical.length;

      const out: Array<[CallSite, Array<[OverlaySlot, boolean]>]> = [];
      for (const tmap of maps) {
        const idx = this._targetMessageIndex(tmap.target, messages);
        if (idx === null || idx >= sections.length) continue;
        const role = messages[idx].role ?? 'user';
        const rendered = messages[idx].content;
        if (typeof rendered !== 'string') continue;
        const spans = introspect.align(tmap.segments, rendered);
        if (spans === null) continue; // validation guard: discard the analysis
        const contentStart = sections[idx].start + role.length + 2;
        const slots = introspect.overlay(tmap.segments, spans, contentStart, boundary);
        const classified: Array<[OverlaySlot, boolean]> = [];
        for (const slot of slots) {
          const valueKey = `${site.file}:${site.line}:${slot.expr}`;
          const value = canonical.slice(slot.start, slot.end);
          const prev = this._slotValues.get(valueKey);
          if (this._slotValues.size >= MAX_SITES && !this._slotValues.has(valueKey)) {
            const oldest = this._slotValues.keys().next().value as string | undefined;
            if (oldest !== undefined) this._slotValues.delete(oldest);
          }
          this._slotValues.set(valueKey, value);
          const dynamic = slot.hasCall || (prev !== undefined && prev !== value);
          classified.push([slot, dynamic]);
        }
        if (classified.length) out.push([site, classified]);
      }
      return out.length ? out : null;
    } catch {
      return null; // fail-open: introspection is a bonus
    }
  }

  private _targetMessageIndex(
    target: string,
    messages: Array<{ role: string; content: string }>,
  ): number | null {
    const role = target === 'system_message' ? 'system' : 'user';
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i] && messages[i].role === role) return i;
    }
    return null;
  }

  private _emitSlotWarnings(
    insights: Array<[CallSite, Array<[OverlaySlot, boolean]>]>,
  ): boolean {
    let shown = 0;
    let extra = 0;
    for (const [site, classified] of insights) {
      const base = basename(site.file);
      for (const [slot, dynamic] of classified) {
        if (!slot.inPrefix || !dynamic) continue;
        const warnKey = `${site.file}:${site.line}:${slot.expr}`;
        if (this._siteWarned.has(warnKey)) continue;
        if (this._siteWarned.size >= MAX_SITES) {
          const oldest = this._siteWarned.keys().next().value as string | undefined;
          if (oldest !== undefined) this._siteWarned.delete(oldest);
        }
        this._siteWarned.set(warnKey, null);
        if (shown >= 3) {
          extra++;
          continue;
        }
        shown++;
        const expr = displayExpr(slot.expr);
        this._emit(
          `${PREFIX} template slot {${expr}} at ${base}:${site.line} ` +
            'renders inside your cacheable prefix — its value changes ' +
            'between calls and breaks the cache there',
        );
        if (slot.resolvedFrom !== null) {
          const fromExpr = displayExpr(slot.resolvedFrom[0]);
          this._emit(`${PREFIX}   {${expr}} = ${fromExpr} at ${base}:${slot.resolvedFrom[1]}`);
        }
      }
    }
    if (extra) {
      this._emit(`${PREFIX}   ...and ${extra} more template slot(s) in the cacheable prefix`);
    }
    return shown > 0 || extra > 0;
  }

  private _emitAttribution(
    insights: Array<[CallSite, Array<[OverlaySlot, boolean]>]>,
    limitedBy: Record<string, any>,
  ): void {
    const bStart = limitedBy.start as number;
    const bEnd = limitedBy.end as number;
    for (const [site, classified] of insights) {
      for (const [slot] of classified) {
        if (slot.start < bEnd && bStart < slot.end) {
          const warnKey = `attr:${site.file}:${site.line}:${slot.expr}`;
          if (this._siteWarned.has(warnKey)) return;
          if (this._siteWarned.size >= MAX_SITES) {
            const oldest = this._siteWarned.keys().next().value as string | undefined;
            if (oldest !== undefined) this._siteWarned.delete(oldest);
          }
          this._siteWarned.set(warnKey, null);
          const base = basename(site.file);
          const expr = displayExpr(slot.expr);
          this._emit(`${PREFIX}   rendered by template slot {${expr}} at ${base}:${site.line}`);
          return; // at most one attribution line per warn block
        }
      }
    }
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
