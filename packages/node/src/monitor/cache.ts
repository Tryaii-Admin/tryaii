/**
 * Cache fields of an event (API.md v1.4 §3.2.1, SPEC.md §3.1). Mirrors
 * monitor/cache.py; the pure helpers are pinned cross-SDK by
 * shared/monitor/fixtures/cache.
 *
 * - `usage.cached_tokens` — the provider's cached prompt tokens, read from
 *   the response usage (cachedTokensFromUsage); recorded on every call.
 * - `cache` — only when the call ran the runtime cache lint
 *   (`wrap(client, { cachelint: true })`): `{outcome, report}`, built per
 *   process by CacheTracker.
 *
 * Redaction: the engine's own finding `excerpt` (the match plus 28 code
 * points of context) is never recorded. A report carries maskExcerpt() of
 * the matched span only, so neither the local events file nor the upload
 * ever holds prompt text.
 *
 * This module has no static value-import of the cachelint engine (its
 * tokenizer rank data is multi-MB): CacheTracker takes the loaded engine.
 */

import { createHash } from 'node:crypto';

import { CpIndex } from '../cachelint/util/codepoints.js';

type Engine = typeof import('../cachelint/index.js');
type AnalyzedItem = import('../cachelint/analyzer.js').AnalyzedItem;

/** Contract limits (API.md §3.2.1). */
export const MAX_FINDINGS = 10;
export const MAX_DETECTOR = 64;
export const MAX_EXCERPT = 40;
export const MAX_RECOMMENDATION = 500;
const ELLIPSIS = '…';
/** Minimum gap between two full analyses (so two reports) of one process (ms). */
export const REPORT_INTERVAL_MS = 60_000;
/** Processes remembered per tracker (FIFO cap). */
export const MAX_PROCESSES = 256;
/** The provider the runtime hook lints as. */
export const LINT_PROVIDER = 'openrouter';

export const SECTION_KINDS = ['tools', 'system', 'messages'] as const;
export type SectionKind = (typeof SECTION_KINDS)[number];
const OUTCOMES: Record<string, string> = { HIT: 'hit', PARTIAL: 'partial', MISS: 'miss' };
const SEVERITIES = new Set(['high', 'medium', 'low']);

export interface CacheFinding {
  severity: string;
  detector: string;
  section: SectionKind;
  offset: number;
  excerpt: string;
}

export interface CacheReport {
  verdict: string;
  prompt_tokens: number;
  stable_prefix_tokens: number;
  floor_tokens: number | null;
  tools_tokens: number;
  system_tokens: number;
  messages_tokens: number;
  findings: CacheFinding[];
  recommendation: string | null;
}

export interface CacheBlock {
  outcome: string | null;
  report: CacheReport | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/** A non-negative integer count, or null (booleans, strings, negatives refused). */
function count(value: unknown): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return null;
  return Math.trunc(value);
}

/**
 * `usage.prompt_tokens_details.cached_tokens` (OpenAI-compatible) or
 * `usage.cache_read_input_tokens` (Anthropic-shaped); null when the provider
 * reported neither (0 = reported, no hit).
 */
export function cachedTokensFromUsage(usage: unknown): number | null {
  if (!isRecord(usage)) return null;
  const details = usage.prompt_tokens_details;
  const cached = isRecord(details) ? count(details.cached_tokens) : null;
  if (cached !== null) return cached;
  return count(usage.cache_read_input_tokens);
}

/**
 * Whitespace per the mask rule: the Unicode White_Space code points, listed
 * explicitly (the same 25 in both SDKs; no Unicode table is consulted).
 */
const WHITESPACE = new Set(
  '\t\n\v\f\r \u0085\u00a0\u1680' +
    '\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a' +
    '\u2028\u2029\u202f\u205f\u3000',
);

/**
 * The matched span, per code point (API.md §3.2.1): ASCII digit → `0`;
 * whitespace → a space; printable ASCII punctuation/symbols (`!`..`~` minus
 * letters and digits) kept; every other code point (ASCII letters, any
 * non-ASCII letter, digit, combining mark, emoji or symbol, controls) → `x`.
 * At most 40 code points (39 + `…` when cut).
 */
export function maskExcerpt(text: string): string {
  const out: string[] = [];
  for (const ch of text) {
    if (ch >= '0' && ch <= '9') out.push('0');
    else if (WHITESPACE.has(ch)) out.push(' ');
    else if (ch >= '!' && ch <= '~' && !((ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z'))) {
      out.push(ch);
    } else out.push('x');
  }
  if (out.length > MAX_EXCERPT) return out.slice(0, MAX_EXCERPT - 1).join('') + ELLIPSIS;
  return out.join('');
}

/** The engine's label of a system-role message (OpenAI `system`/`developer`). */
const RE_SYSTEM_MESSAGE = /^messages\[[0-9]+\]:(?:system|developer)$/;

/**
 * The report section of an engine section label (API.md §3.2.1): a message
 * whose role is `system` or `developer` (`messages[0]:system`,
 * `messages[2]:developer`) → `system`; otherwise the label cut at the first
 * `[` or `:` (`tools` → tools, `system` → system, `messages[2]:user` and
 * anything unexpected → messages).
 */
export function sectionKind(label: unknown): SectionKind {
  let text = typeof label === 'string' ? label : String(label ?? '');
  if (RE_SYSTEM_MESSAGE.test(text)) return 'system';
  for (const sep of ['[', ':']) {
    const idx = text.indexOf(sep);
    if (idx >= 0) text = text.slice(0, idx);
  }
  return (SECTION_KINDS as readonly string[]).includes(text) ? (text as SectionKind) : 'messages';
}

function untag(line: string): string {
  const idx = line.indexOf('] ');
  return idx >= 0 ? line.slice(idx + 2) : line;
}

/**
 * The first `[fix:*]` line, else (CACHEABLE_WITH_ACTION only) the
 * `[enablement]` line, with its `[tag] ` removed; null otherwise. Capped at
 * 500 code points.
 */
export function recommendationFrom(recommendations: unknown, verdict: unknown): string | null {
  const lines = Array.isArray(recommendations)
    ? recommendations.filter((r): r is string => typeof r === 'string')
    : [];
  let chosen = lines.find((r) => r.startsWith('[fix:'));
  if (chosen === undefined && verdict === 'CACHEABLE_WITH_ACTION') {
    chosen = lines.find((r) => r.startsWith('[enablement]'));
  }
  if (chosen === undefined) return null;
  const points = Array.from(untag(chosen));
  if (points.length > MAX_RECOMMENDATION) {
    return points.slice(0, MAX_RECOMMENDATION - 1).join('') + ELLIPSIS;
  }
  return points.join('');
}

/** Pair prediction → event outcome (UNKNOWN, AT_RISK → null). */
export function outcomeFromExpected(expected: unknown): string | null {
  return typeof expected === 'string' && expected in OUTCOMES ? OUTCOMES[expected] : null;
}

/**
 * The cachelint prompt of one chat.completions request: `messages`, plus
 * `tools` when the request declares tool schemas (its `tools` list followed
 * by the legacy `functions` list). The engine renders `tools` first
 * (shared/cachelint/SPEC.md §1.4), so the schemas count in `prompt_tokens`,
 * `tools_tokens`, the stable prefix and the verdict.
 */
export function lintPrompt(
  messages: unknown,
  tools?: unknown,
  functions?: unknown,
): { messages: unknown; tools?: unknown[] } {
  const prompt: { messages: unknown; tools?: unknown[] } = { messages };
  const schemas = [
    ...(Array.isArray(tools) ? tools : []),
    ...(Array.isArray(functions) ? functions : []),
  ];
  if (schemas.length > 0) prompt.tools = schemas;
  return prompt;
}

/**
 * A CacheReport (API.md §3.2.1) from one analysed item (the engine's
 * `items[i]`) and the canonical text it was computed on.
 */
export function buildReport(data: Record<string, any>, canonical: string): CacheReport {
  const tokens = (data.token_report ?? {}) as Record<string, any>;
  const sections: Record<SectionKind, number> = { tools: 0, system: 0, messages: 0 };
  for (const [label, n] of Object.entries((tokens.sections ?? {}) as Record<string, unknown>)) {
    sections[sectionKind(label)] += count(n) ?? 0;
  }
  const cp = new CpIndex(canonical);
  const findings: CacheFinding[] = [];
  for (const f of ((data.findings ?? []) as Array<Record<string, any>>).slice(0, MAX_FINDINGS)) {
    const start = Math.trunc(Number(f.start));
    const end = Math.trunc(Number(f.end));
    findings.push({
      severity: SEVERITIES.has(f.severity) ? f.severity : 'low',
      detector: Array.from(String(f.kind ?? '')).slice(0, MAX_DETECTOR).join(''),
      section: sectionKind(f.section),
      offset: Math.max(0, start),
      excerpt: maskExcerpt(cp.slice(start, end)),
    });
  }
  const verdict = (data.verdict ?? {}).code as string;
  const threshold = (data.threshold ?? {}) as Record<string, unknown>;
  return {
    verdict,
    prompt_tokens: count((tokens.total ?? {}).tokens) ?? 0,
    stable_prefix_tokens: count((data.stable_prefix ?? {}).tokens) ?? 0,
    floor_tokens: count(threshold.min_tokens),
    tools_tokens: sections.tools,
    system_tokens: sections.system,
    messages_tokens: sections.messages,
    findings,
    recommendation: recommendationFrom(data.recommendations, verdict),
  };
}

// ---------------------------------------------------------------------------
// Per-process tracking
// ---------------------------------------------------------------------------

interface ProcessState {
  prev: AnalyzedItem | null;
  prevGroup: string | null;
  report: CacheReport | null;
  /** Shape of the last analysed call. */
  reportKey: string | null;
  /** Clock of the last full analysis; null before the first. */
  analysedAt: number | null;
}

function shapeKey(model: string, canonical: string): string {
  return createHash('sha256').update(model + '\x00' + canonical, 'utf-8').digest('hex');
}

/**
 * The `cache` block per process (API.md §3.2.1): the pair prediction from
 * the previous linted call of the same process, and a report on the first
 * call and then when it changed and 60 s have passed.
 *
 * The full analysis (`analyzeItem`: detectors, token report, verdict) runs
 * on the first call of a process, then at most once per 60 s per process and
 * only when the prompt's shape changed, whether or not the resulting report
 * differs from the last one sent. The pair prediction runs on every call; it
 * is the cheap path (a common-prefix scan and one token count of that
 * prefix, no detectors). `analyses` counts full analyses. Lives as long as
 * its Monitor (one SDK process). `clock` returns milliseconds.
 */
export class CacheTracker {
  private readonly procs = new Map<string, ProcessState>();
  /** Full analyses run so far (all processes). */
  analyses = 0;

  constructor(
    private readonly engine: Engine,
    private readonly clock: () => number = () => performance.now(),
  ) {}

  observe(
    process: string,
    model: string,
    messages: unknown[],
    tools?: unknown,
    functions?: unknown,
  ): CacheBlock {
    const e = this.engine;
    const prompt = lintPrompt(messages, tools, functions);
    const [canonical, sections] = e.buildCanonical(prompt);
    const resolved = e.resolve(LINT_PROVIDER, model);
    const group = e.normModel(model);
    const curr: AnalyzedItem = {
      data: { index: 0, model },
      canonical,
      sections,
      resolved,
      sent_at: null,
    };
    const key = shapeKey(model, canonical);
    let state = this.procs.get(process);
    if (state === undefined) {
      if (this.procs.size >= MAX_PROCESSES) {
        const oldest = this.procs.keys().next().value as string | undefined;
        if (oldest !== undefined) this.procs.delete(oldest);
      }
      state = { prev: null, prevGroup: null, report: null, reportKey: null, analysedAt: null };
      this.procs.set(process, state);
    }
    const { prev, prevGroup } = state;
    state.prev = curr;
    state.prevGroup = group;
    const now = this.clock();
    // At most one full analysis per 60 s per process, whether or not the
    // report it produces differs from the last one sent.
    const analyse =
      state.analysedAt === null ||
      (now - state.analysedAt >= REPORT_INTERVAL_MS && key !== state.reportKey);
    if (analyse) {
      state.analysedAt = now;
      state.reportKey = key;
      this.analyses += 1;
    }

    let outcome: string | null = null;
    if (prev !== null && prevGroup === group) {
      outcome = outcomeFromExpected(e.predictPair(prev, curr).expected);
    }
    let report: CacheReport | null = null;
    if (analyse) {
      const item = e.analyzeItem({ prompt, llm: { provider: LINT_PROVIDER, name: model } });
      const candidate = buildReport(item.data, item.canonical);
      if (state.report === null || JSON.stringify(candidate) !== JSON.stringify(state.report)) {
        report = candidate;
        state.report = candidate;
      }
    }
    return { outcome, report };
  }
}
