/**
 * Sequence (multi-input) analysis: relations between consecutive requests.
 * TypeScript mirror of the Python reference — grouping by (provider,
 * normalized model) (SPEC.md delta m), code-point LCPs (SPEC.md §1.1), TTL
 * gaps via the strict shared ISO-8601 grammar (SPEC.md §1.6).
 */

import { scan } from './detectors.js';
import { normModel } from './providers.js';
import { countTokens } from './tokenizers.js';
import { CpIndex, cpLcp } from './util/codepoints.js';
import { formatFixed, halfEvenRound } from './util/halfEven.js';
import { parseTsMs } from './util/iso8601.js';
import { sectionOf, type AnalyzedItem } from './analyzer.js';

const DIVERGE_WINDOW_BACK = 40;
const DIVERGE_WINDOW_FWD = 100;

function context(cp: CpIndex, offset: number): string {
  const lo = Math.max(0, offset - DIVERGE_WINDOW_BACK);
  const hi = Math.min(cp.length, offset + DIVERGE_WINDOW_FWD);
  return cp.slice(lo, hi).split('\n').join('\\n');
}

function setsEqual(a: Set<string>, b: Set<string>): boolean {
  if (a.size !== b.size) return false;
  for (const v of a) if (!b.has(v)) return false;
  return true;
}

/**
 * Which detector kinds fire around the divergence point, and did their VALUES
 * change? A kind present in both windows with identical matched text is just
 * nearby content, not the cause.
 */
function classifyCause(a: string, b: string, lcp: number): string[] {
  const values = new Map<string, { a: Set<string>; b: Set<string> }>();
  for (const [label, text] of [
    ['a', a],
    ['b', b],
  ] as const) {
    const cp = new CpIndex(text);
    const lo = Math.max(0, lcp - DIVERGE_WINDOW_BACK);
    const hi = Math.min(cp.length, lcp + DIVERGE_WINDOW_FWD);
    const window = cp.slice(lo, hi);
    const wcp = new CpIndex(window);
    for (const f of scan(window)) {
      const matched = wcp.slice(f.start, f.end);
      if (!values.has(f.kind)) values.set(f.kind, { a: new Set(), b: new Set() });
      (values.get(f.kind) as { a: Set<string>; b: Set<string> })[label].add(matched);
    }
  }
  const causes: string[] = [];
  for (const kind of [...values.keys()].sort()) {
    const sides = values.get(kind) as { a: Set<string>; b: Set<string> };
    if (sides.a.size && sides.b.size) {
      if (!setsEqual(sides.a, sides.b)) causes.push(`${kind} value changed`);
      // identical values on both sides: nearby content, not the cause
    } else if (sides.b.size) {
      causes.push(`${kind} introduced`);
    } else if (sides.a.size) {
      causes.push(`${kind} removed`);
    }
  }
  return causes;
}

function pairAnalysis(prev: AnalyzedItem, curr: AnalyzedItem): Record<string, unknown> {
  const a = prev.canonical;
  const b = curr.canonical;
  const providerKey = prev.resolved.provider_key;
  const upstream = prev.resolved.upstream;
  const minTokens = prev.resolved.min_tokens;
  const spec = prev.resolved.spec;
  const aCp = new CpIndex(a);
  const bCp = new CpIndex(b);

  const lcp = cpLcp(a, b);
  const lcpTokens = countTokens(aCp.slice(0, lcp), providerKey, upstream).tokens;
  const notes: string[] = [];
  let divergedAt: Record<string, unknown> | null = null;
  let causes: string[] = [];

  let relation: string;
  if (a === b) {
    relation = 'identical';
  } else if (lcp === aCp.length) {
    relation = 'append-only'; // b extends a: the classic growing conversation
  } else if (lcp === bCp.length) {
    relation = 'truncated'; // b is a prefix of a: history was trimmed
    notes.push(
      'The newer request is a PREFIX of the older one — history trimming? ' +
        'Its full length can still hit the cache, but trimming mid-conversation ' +
        'usually signals a context-window strategy that fights caching.',
    );
  } else {
    relation = 'diverged';
    const sec = sectionOf(prev.sections, Math.min(lcp, Math.max(0, aCp.length - 1)));
    causes = classifyCause(a, b, lcp);
    divergedAt = {
      offset: lcp,
      section: sec ? sec.name : '?',
      section_offset: sec ? lcp - sec.start : null,
      context_prev: context(aCp, lcp),
      context_curr: context(bCp, lcp),
    };
    if (sec && sec.name === 'tools') {
      notes.push(
        'Divergence inside the TOOLS section — tool changes invalidate the ' +
          'ENTIRE cache (tools render first).',
      );
    } else if (sec && sec.name === 'system') {
      notes.push(
        'Divergence inside the SYSTEM section — invalidates system + messages ' +
          '(tools may survive on Anthropic).',
      );
    }
  }

  // expected outcome
  const thresholdOk = minTokens === null || lcpTokens >= minTokens;
  let expected: string;
  let expectedCached: number | null;
  if (relation === 'identical' || relation === 'append-only') {
    expected = thresholdOk ? 'HIT' : 'MISS';
    expectedCached = thresholdOk ? lcpTokens : 0;
    if (!thresholdOk) {
      notes.push(
        `Common prefix ~${lcpTokens} tok is below the ${minTokens} minimum — ` +
          'no cache credit despite the clean prefix.',
      );
    }
  } else if (relation === 'truncated') {
    expected = thresholdOk ? 'HIT' : 'MISS';
    expectedCached = thresholdOk ? lcpTokens : 0;
  } else {
    if (thresholdOk && minTokens !== null) {
      expected = 'PARTIAL';
      expectedCached = lcpTokens;
      notes.push(
        `Only the ~${lcpTokens}-tok common prefix gets cache credit; ` +
          'everything after the divergence re-processes at full price.',
      );
    } else if (minTokens === null) {
      expected = 'UNKNOWN';
      expectedCached = null;
    } else {
      expected = 'MISS';
      expectedCached = 0;
      notes.push(
        `Common prefix ~${lcpTokens} tok < ${minTokens} minimum — ` +
          'the divergence effectively kills caching for this pair.',
      );
    }
  }

  // TTL gap check (SPEC.md §1.6: strict shared grammar, epoch-ms arithmetic)
  let ttlCheck: Record<string, unknown> | null = null;
  const tPrev = parseTsMs(prev.sent_at);
  const tCurr = parseTsMs(curr.sent_at);
  if (tPrev !== null && tCurr !== null) {
    const gap = (tCurr - tPrev) / 1000.0;
    ttlCheck = { gap_seconds: halfEvenRound(gap, 1), ttl_seconds: spec.ttl_seconds };
    if (spec.ttl_seconds === null) {
      ttlCheck.note =
        'No TTL guarantee / unpublished lifetime for this provider — any gap is at-risk.';
      if (expected === 'HIT') expected = 'AT_RISK';
    } else if (gap > spec.ttl_seconds && (expected === 'HIT' || expected === 'PARTIAL')) {
      if (providerKey === 'openai' && gap <= 24 * 3600) {
        expected = 'AT_RISK';
        ttlCheck.note =
          `Gap ${formatFixed(gap, 0)}s exceeds the 5-10 min sliding window; may ` +
          'still hit via 24h extended retention (not guaranteed).';
      } else {
        expected = 'MISS';
        ttlCheck.note =
          `Gap ${formatFixed(gap, 0)}s exceeds the default TTL ` +
          `(${spec.ttl_seconds}s) — entry likely evicted. ` +
          'Consider the 1h TTL / keep-alive pings where available.';
      }
    }
  }

  if (spec.enablement === 'explicit' && ['HIT', 'PARTIAL', 'AT_RISK'].includes(expected)) {
    notes.push(
      'Explicit-enablement provider: the hit requires a cache breakpoint ' +
        'placed at/inside the common prefix on BOTH requests.',
    );
  }

  return {
    relation,
    lcp_chars: lcp,
    lcp_tokens: lcpTokens,
    expected,
    expected_cached_tokens: expectedCached,
    diverged_at: divergedAt,
    likely_causes: causes,
    ttl_check: ttlCheck,
    notes,
  };
}

/** Group by (provider, normalized model) and analyze consecutive-pair relations. */
export function analyzeSequences(items: AnalyzedItem[]): Record<string, unknown>[] {
  const groups = new Map<string, AnalyzedItem[]>();
  for (const it of items) {
    // SPEC.md delta m: normalize the model for grouping; the group header
    // still displays the first-seen raw string.
    const key = JSON.stringify([it.resolved.provider_key, normModel(it.data.model as string)]);
    if (!groups.has(key)) groups.set(key, []);
    (groups.get(key) as AnalyzedItem[]).push(it);
  }

  const results: Record<string, unknown>[] = [];
  for (const members of groups.values()) {
    const providerKey = members[0].resolved.provider_key;
    const model = members[0].data.model as string;
    const spec = members[0].resolved.spec;
    const minTokens = members[0].resolved.min_tokens;

    const groupResult: Record<string, unknown> = {
      group: { provider: spec.display, provider_key: providerKey, model },
      count: members.length,
      indices: members.map((m) => m.data.index),
      pairs: [] as Record<string, unknown>[],
      shared_prefix_all: null as Record<string, unknown> | null,
      summary: '',
    };

    if (members.length === 1) {
      groupResult.summary =
        'Single request — caching needs >= 2 same-prefix requests inside the TTL window ' +
        'to pay off (a lone write costs the premium for nothing where one exists).';
      results.push(groupResult);
      continue;
    }

    // LCP across ALL members = the fleet-wide shared cache unit
    const firstCp = new CpIndex(members[0].canonical);
    let sharedLen = firstCp.length;
    for (const m of members.slice(1)) {
      sharedLen = Math.min(sharedLen, cpLcp(firstCp.slice(0, sharedLen), m.canonical));
    }
    const sharedTokens = countTokens(
      firstCp.slice(0, sharedLen),
      providerKey,
      members[0].resolved.upstream,
    ).tokens;
    groupResult.shared_prefix_all = {
      tokens: sharedTokens,
      meets_threshold: minTokens === null || sharedTokens >= minTokens,
      min_tokens: minTokens,
    };

    let hits = 0;
    const pairs = groupResult.pairs as Record<string, unknown>[];
    for (let i = 1; i < members.length; i++) {
      const pair = pairAnalysis(members[i - 1], members[i]);
      pair.from = members[i - 1].data.index;
      pair.to = members[i].data.index;
      pairs.push(pair);
      if (pair.expected === 'HIT' || pair.expected === 'PARTIAL') hits++;
    }

    const nPairs = pairs.length;
    const ok = (groupResult.shared_prefix_all as Record<string, unknown>).meets_threshold
      ? 'clears'
      : 'does NOT clear';
    groupResult.summary =
      `${members.length} requests; ${hits}/${nPairs} consecutive transitions expected to get ` +
      `cache credit. Shared prefix across all requests: ~${sharedTokens} tok, which ${ok} ` +
      'the activation threshold' +
      (minTokens !== null ? ` (${minTokens}).` : ' (unpublished threshold).');
    results.push(groupResult);
  }

  if (groups.size > 1) {
    results.push({
      group: { provider: '-', provider_key: '-', model: '-' },
      count: 0,
      indices: [],
      pairs: [],
      shared_prefix_all: null,
      summary:
        'NOTE: the input list spans multiple (provider, model) pairs — caches ' +
        'are model- and provider-scoped, so requests in different groups never ' +
        'share cache. A model/provider switch mid-sequence = cold cache.',
    });
  }
  return results;
}
