/**
 * Single-prompt cache analysis — TypeScript mirror of the Python reference.
 *
 * Pipeline per input:
 *   1. canonical render (tools -> system -> messages, the documented provider order)
 *   2. token counts (total + per section, method-labeled)
 *   3. dynamic-content findings with section attribution
 *   4. stable-prefix computation (tokens before the first blocking finding)
 *   5. verdict + provider-aware recommendations
 *
 * Every user-facing string and every output key ORDER is byte-identical to
 * the Python engine (the --json outputs are diffed directly).
 */

import { blocking, findingToDict, firstBlocking, scan, type Finding } from './detectors.js';
import { resolve, type Resolved } from './providers.js';
import { countTokens } from './tokenizers.js';
import { CpIndex, cpLength } from './util/codepoints.js';
import { formatFixed, halfEvenRound } from './util/halfEven.js';
import { sortedStringify } from './util/stableJson.js';

export interface Section {
  name: string;
  start: number; // code points
  end: number; // code points
}

/** Analysis result plus internals the sequence analyzer reuses. */
export interface AnalyzedItem {
  data: Record<string, unknown>;
  canonical: string;
  sections: Section[];
  resolved: Resolved;
  sent_at: string | null;
}

// ---------------------------------------------------------------------------
// Input normalization + canonical rendering
// ---------------------------------------------------------------------------

/** Python truthiness for the canonical-render skips (empty str/list/dict are falsy). */
function pyTruthy(v: unknown): boolean {
  if (v === null || v === undefined || v === false || v === '' || v === 0) return false;
  if (Array.isArray(v)) return v.length > 0;
  if (typeof v === 'object') return Object.keys(v as object).length > 0;
  return true;
}

function extractText(content: unknown): [string, number] {
  if (content === null || content === undefined) return ['', 0];
  if (typeof content === 'string') return [content, 0];
  if (Array.isArray(content)) {
    const parts: string[] = [];
    let nonText = 0;
    for (const block of content) {
      if (typeof block === 'string') {
        parts.push(block);
      } else if (block !== null && typeof block === 'object' && !Array.isArray(block)) {
        const b = block as Record<string, unknown>;
        if (b.type === 'text' || 'text' in b) {
          parts.push(String(b.text ?? ''));
        } else {
          nonText++;
          parts.push(`[${b.type ?? 'block'}]`);
        }
      } else {
        parts.push(String(block));
      }
    }
    return [parts.join('\n'), nonText];
  }
  return [String(content), 0];
}

/**
 * Render the prompt into one deterministic string with section spans.
 * Order: tools -> system -> messages. Returns [text, sections, nonTextCount].
 */
export function buildCanonical(prompt: unknown): [string, Section[], number] {
  let p: Record<string, unknown>;
  if (typeof prompt === 'string') {
    p = { messages: [{ role: 'user', content: prompt }] };
  } else if (prompt !== null && typeof prompt === 'object' && !Array.isArray(prompt)) {
    p = prompt as Record<string, unknown>;
  } else {
    throw new Error('prompt must be a string or an object with system/messages/tools');
  }

  const pieces: [string, string][] = [];
  let nonTextTotal = 0;

  const tools = p.tools;
  if (pyTruthy(tools)) {
    // Canonical serialization: minified, sorted keys, integral floats as
    // ints (SPEC.md §1.4) — byte-identical to the Python rendering.
    pieces.push(['tools', sortedStringify(tools)]);
  }

  const system = p.system;
  if (pyTruthy(system)) {
    const [text, nt] = extractText(system);
    nonTextTotal += nt;
    pieces.push(['system', text]);
  }

  const messages = (p.messages as unknown[]) || [];
  for (let i = 0; i < messages.length; i++) {
    const msg = messages[i];
    let role: string;
    let text: string;
    if (msg !== null && typeof msg === 'object' && !Array.isArray(msg)) {
      const m = msg as Record<string, unknown>;
      role = (m.role as string) ?? 'user';
      const [t, nt] = extractText(m.content);
      text = t;
      nonTextTotal += nt;
    } else {
      role = 'user';
      text = String(msg);
    }
    pieces.push([`messages[${i}]:${role}`, `${role}: ${text}`]);
  }

  const sections: Section[] = [];
  const out: string[] = [];
  let pos = 0;
  for (const [name, text] of pieces) {
    if (out.length) {
      out.push('\n\n');
      pos += 2;
    }
    const start = pos;
    out.push(text);
    pos += cpLength(text);
    sections.push({ name, start, end: pos });
  }
  return [out.join(''), sections, nonTextTotal];
}

/**
 * Section containing `offset`; separator / past-end offsets map to the
 * nearest PRECEDING section (SPEC.md delta l).
 */
export function sectionOf(sections: Section[], offset: number): Section | null {
  let prev: Section | null = null;
  for (const sec of sections) {
    if (offset < sec.start) break;
    prev = sec;
    if (sec.start <= offset && offset < Math.max(sec.end, sec.start + 1)) return sec;
  }
  return prev !== null ? prev : sections.length ? sections[0] : null;
}

// ---------------------------------------------------------------------------
// Verdict + recommendations
// ---------------------------------------------------------------------------

const FIX_HINTS: Record<string, string> = {
  'timestamp-datetime': 'Remove it, round it to a coarse bucket (day/hour), or move it into the final user message (after the cached prefix).',
  'timestamp-epoch': 'Remove the epoch value or move it after the static prefix.',
  'uuid': 'Move per-session IDs after the static prefix — or drop them from the prompt entirely.',
  'session-id': 'Move per-session/user IDs to the tail (final user message); they prevent any cross-request prefix reuse where they sit.',
  'secret-like': 'Remove the secret from the prompt (pass credentials out-of-band). This is a security issue independent of caching.',
  'date-phrase': "Inject 'today' only in the final user message, or round to the day and accept a daily cold write.",
  'date-iso': "If this is an injected 'today', move it to the tail; if it's static content, ignore this finding.",
  'date-us': "If this is an injected 'today', move it to the tail; if it's static content, ignore this finding.",
  'date-verbose': "If this is an injected 'today', move it to the tail; if it's static content, ignore this finding.",
  'template-jinja': 'Template slot: everything BEFORE it stays cacheable — keep all slots that vary per call at the tail of the prompt.',
  'template-braces': 'Template slot: keep per-call slots at the tail; slots filled with the SAME value every call are harmless.',
  'template-dollar': 'Template slot: keep per-call slots at the tail of the prompt.',
  'template-percent-named': 'Template slot: keep per-call slots at the tail of the prompt.',
  'template-angle': 'Placeholder: keep per-call placeholders at the tail of the prompt.',
  'template-bracket': 'Placeholder: fill it with a stable value or keep it at the tail.',
};

function verdict(
  resolved: Resolved,
  totalTokens: number,
  stableTokens: number,
  hasBlockers: boolean,
): [string, string] {
  const spec = resolved.spec;
  const minTokens = resolved.min_tokens;
  if (minTokens === null) {
    return [
      'UNKNOWN_THRESHOLD',
      `${spec.display} publishes no activation threshold — analysis is structural only. ` +
        `Stable prefix ~${stableTokens} tok of ${totalTokens} total.`,
    ];
  }
  if (totalTokens < minTokens) {
    return [
      'BELOW_THRESHOLD',
      `Total prompt ~${totalTokens} tok < ${minTokens} minimum — nothing will cache. ` +
        'Expanding the static content to reach the floor is often worthwhile.',
    ];
  }
  if (hasBlockers && stableTokens < minTokens) {
    return [
      'EFFECTIVELY_UNCACHEABLE',
      `Dynamic content too early: the stable prefix is only ~${stableTokens} tok, ` +
        `below the ${minTokens} minimum — in practice nothing will cache until it moves.`,
    ];
  }
  if (hasBlockers) {
    return [
      'CACHEABLE_PREFIX',
      `A stable prefix of ~${stableTokens} tok (>= ${minTokens} minimum) can cache; ` +
        'the dynamic tail after it re-processes at full price each call (that part is normal).',
    ];
  }
  // SPEC.md delta i: any Anthropic upstream needs the explicit cache_control
  // action — pass-through (openrouter) AND hybrid (vertex+claude) alike.
  const needsAction = spec.enablement === 'explicit' || resolved.upstream === 'anthropic';
  if (needsAction) {
    const who =
      spec.enablement === 'explicit' ? spec.display : `${spec.display} (Anthropic upstream)`;
    return [
      'CACHEABLE_WITH_ACTION',
      `~${totalTokens} tok, no dynamic content detected — cacheable, but ${who} ` +
        'requires an explicit opt-in (see enablement).',
    ];
  }
  return [
    'CACHEABLE',
    `~${totalTokens} tok of stable content, above the ${minTokens} minimum — should cache.`,
  ];
}

function recommendations(
  resolved: Resolved,
  verdictCode: string,
  findings: Finding[],
  stableTokens: number,
  nextStable: number | null,
): string[] {
  const spec = resolved.spec;
  const recs: string[] = [];

  if (spec.action) recs.push(`[enablement] ${spec.action}`);

  const blockers = blocking(findings);
  for (const f of blockers.slice(0, 6)) {
    const hint = FIX_HINTS[f.kind] ?? 'Move this value after the static prefix.';
    recs.push(
      `[fix:${f.kind}] at ${f.section}+${f.section_offset} ` +
        `(${formatFixed(f.pct_into_prompt, 0)}% in): ${hint}`,
    );
  }
  if (blockers.length > 6) {
    recs.push(`[fix] ...and ${blockers.length - 6} more findings (see the findings list).`);
  }

  if (blockers.length && nextStable !== null) {
    recs.push(
      `[impact] Fixing the FIRST finding alone extends the stable prefix from ` +
        `~${stableTokens} to ~${nextStable} tok.`,
    );
  }

  if (spec.routing) recs.push(`[routing] ${spec.routing}`);

  recs.push(
    `[verify] After deploying, assert ${spec.verify_field} > 0 in responses. ` +
      '0 across identical-prefix requests = a silent invalidator is still present.',
  );

  if (['CACHEABLE', 'CACHEABLE_WITH_ACTION', 'CACHEABLE_PREFIX'].includes(verdictCode)) {
    recs.push(`[ttl] ${spec.ttl_summary}`);
  }
  if (spec.batch_note) recs.push(`[batch] ${spec.batch_note}`);
  return recs;
}

// ---------------------------------------------------------------------------
// Hygiene seam (public — consumed by the diagnose engine)
// ---------------------------------------------------------------------------

export const DEFAULT_FIX_HINT = 'Move this value after the static prefix.';

/**
 * Provider-independent hygiene scan of a prompt.
 *
 * Canonical render + detector scan + section attribution + per-kind fix
 * hints — no provider KB, no tokenizer, so it works for sites that declare
 * no provider. Findings carry the standard detector dict plus a `hint`.
 * Mirrors the Python reference (cachelint/analyzer.py hygiene_findings).
 */
export function hygieneFindings(prompt: unknown): Record<string, unknown> {
  const [canonical, sections] = buildCanonical(prompt);
  const findings = scan(canonical);
  for (const f of findings) {
    const sec = sectionOf(sections, f.start);
    if (sec) {
      f.section = sec.name;
      f.section_offset = f.start - sec.start;
    }
  }
  const out = findings.map((f) => ({
    ...findingToDict(f),
    hint: FIX_HINTS[f.kind] ?? DEFAULT_FIX_HINT,
  }));
  return {
    findings: out,
    blocking_count: blocking(findings).length,
    findings_in_system: findings.filter((f) => f.section === 'system').length,
    has_system: sections.some((s) => s.name === 'system'),
    has_tools: sections.some((s) => s.name === 'tools'),
    message_count: sections.filter((s) => s.name.startsWith('messages[')).length,
  };
}

// ---------------------------------------------------------------------------
// Main entry
// ---------------------------------------------------------------------------

/**
 * Start offset of the earliest blocker OTHER than `first` — excluded by
 * identity, not by offset (SPEC.md delta g).
 */
export function nextBlockingStart(blockers: Finding[], first: Finding): number | null {
  const rest = blockers.filter((b) => b !== first);
  if (!rest.length) return null;
  let best = rest[0].start;
  for (const b of rest) if (b.start < best) best = b.start;
  return best;
}

/** Analyze one {prompt, llm:{provider,name}} input. */
export function analyzeItem(item: Record<string, unknown>, index = 0): AnalyzedItem {
  const llm = (item.llm as Record<string, unknown>) || {};
  const provider = (llm.provider as string) || (item.provider as string) || '';
  const model = (llm.name as string) || (llm.model as string) || (item.model as string) || '';
  if (!provider) {
    throw new Error(`input #${index}: missing llm.provider`);
  }

  const resolved = resolve(provider, model);
  const spec = resolved.spec;
  const upstream = resolved.upstream;

  const [canonical, sections, nonText] = buildCanonical(item.prompt);
  const cp = new CpIndex(canonical);

  // token counts: total + per section
  const totalTc = countTokens(canonical, resolved.provider_key, upstream);
  const sectionCounts: Record<string, number> = {};
  for (const sec of sections) {
    const tc = countTokens(cp.slice(sec.start, sec.end), resolved.provider_key, upstream);
    sectionCounts[sec.name] = tc.tokens;
  }

  // findings with section attribution
  const findings = scan(canonical);
  for (const f of findings) {
    const sec = sectionOf(sections, f.start);
    if (sec) {
      f.section = sec.name;
      f.section_offset = f.start - sec.start;
    }
  }

  const blockers = blocking(findings);
  const first = firstBlocking(findings);
  let stableTokens: number;
  let nextStable: number | null;
  if (first !== null) {
    stableTokens = countTokens(cp.slice(0, first.start), resolved.provider_key, upstream).tokens;
    // hypothetical: prefix if the first blocker were fixed
    const nxtStart = nextBlockingStart(blockers, first);
    if (nxtStart !== null) {
      nextStable = countTokens(cp.slice(0, nxtStart), resolved.provider_key, upstream).tokens;
    } else {
      nextStable = totalTc.tokens;
    }
  } else {
    stableTokens = totalTc.tokens;
    nextStable = null;
  }

  // the classic cached unit: tools + system
  let cachedUnitTokens = 0;
  for (const [name, n] of Object.entries(sectionCounts)) {
    if (name === 'tools' || name === 'system') cachedUnitTokens += n;
  }
  const systemFindings = findings.filter((f) => f.section === 'system');

  const minTokens = resolved.min_tokens;
  const [verdictCode, verdictSummary] = verdict(
    resolved,
    totalTc.tokens,
    stableTokens,
    blockers.length > 0,
  );
  const recs = recommendations(resolved, verdictCode, findings, stableTokens, nextStable);

  const warnings = [...resolved.warnings];
  if (nonText) {
    warnings.push(
      `${nonText} non-text block(s) (images/documents) approximated as ` +
        'placeholders — token counts underestimate them.',
    );
  }
  if (!totalTc.exact) {
    warnings.push(`Token counts are estimates (${totalTc.method}). ${totalTc.note}`);
  }

  const data: Record<string, unknown> = {
    index,
    provider: spec.display,
    provider_key: resolved.provider_key,
    upstream,
    model,
    token_report: {
      total: { tokens: totalTc.tokens, method: totalTc.method, exact: totalTc.exact, note: totalTc.note },
      sections: sectionCounts,
    },
    threshold: {
      min_tokens: minTokens,
      tier: resolved.tier_note,
      increment: spec.increment_tokens,
      meets: minTokens !== null && totalTc.tokens >= minTokens,
    },
    system_report: {
      present: sections.some((s) => s.name === 'system'),
      tokens: sectionCounts['system'] ?? 0,
      cached_unit_tokens: cachedUnitTokens, // tools + system
      cached_unit_meets_threshold: minTokens !== null && cachedUnitTokens >= minTokens,
      findings_in_system: systemFindings.length,
    },
    findings: findings.map(findingToDict),
    stable_prefix: {
      tokens: stableTokens,
      pct_of_prompt: halfEvenRound((100.0 * stableTokens) / Math.max(1, totalTc.tokens), 1),
      limited_by: first ? findingToDict(first) : null,
      if_first_fixed_tokens: nextStable,
    },
    verdict: { code: verdictCode, summary: verdictSummary },
    enablement: {
      mode: spec.enablement,
      detail: spec.enablement_detail,
      action: spec.action,
    },
    provider_intel: {
      ttl: spec.ttl_summary,
      read_discount: spec.read_discount,
      write_cost: spec.write_cost,
      verify_field: spec.verify_field,
      routing: spec.routing,
      batch: spec.batch_note,
      gotchas: [...spec.gotchas],
      sources: [...spec.sources],
    },
    recommendations: recs,
    warnings,
  };
  return {
    data,
    canonical,
    sections,
    resolved,
    sent_at: (item.sent_at as string) ?? null,
  };
}
