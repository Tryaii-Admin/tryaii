/**
 * Dynamic-content detectors (18 kinds) — the TypeScript translation of the
 * Python reference detectors (SPEC.md §1.7).
 *
 * Python's Unicode-aware \b becomes explicit lookarounds on the word class
 * [\p{L}\p{M}\p{N}_] with the u flag (JS \b is ASCII-only, so a Hebrew letter
 * adjacent to a date must NOT create a boundary — exactly as in Python).
 * Leading (?i) becomes the i flag. All offsets are Unicode code points,
 * converted from the UTF-16 regex match indices (SPEC.md §1.1).
 *
 * high + medium block the stable prefix; low is advisory only.
 */

import { halfEvenRound } from './util/halfEven.js';
import { CpIndex } from './util/codepoints.js';

export interface Finding {
  kind: string;
  severity: 'high' | 'medium' | 'low';
  start: number; // code points
  end: number; // code points
  excerpt: string;
  why: string;
  section: string;
  section_offset: number;
  pct_into_prompt: number; // raw (unrounded) — toDict rounds
}

interface Detector {
  kind: string;
  severity: 'high' | 'medium' | 'low';
  pattern: RegExp; // must carry the g flag
  why: string;
  validate?: (matched: string) => boolean;
}

/** Serialize a finding in the frozen key order (SPEC.md §1.3 integer rule applied). */
export function findingToDict(f: Finding): Record<string, unknown> {
  return {
    kind: f.kind,
    severity: f.severity,
    start: f.start,
    end: f.end,
    section: f.section,
    section_offset: f.section_offset,
    pct_into_prompt: halfEvenRound(f.pct_into_prompt, 1),
    excerpt: f.excerpt,
    why: f.why,
  };
}

function hexMixed(s: string): boolean {
  return /[0-9]/.test(s) && /[a-zA-Z]/.test(s);
}

// Python \b emulation: boundary before/after a word character.
const NW = '[\\p{L}\\p{M}\\p{N}_]'; // the Unicode word class
const B_BEFORE = `(?<!${NW})`; // no word char immediately before
const B_AFTER = `(?!${NW})`; // no word char immediately after

const MONTHS =
  '(?:January|February|March|April|May|June|July|August|September|' +
  'October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)';

const DETECTORS: readonly Detector[] = [
  // --- values that certainly change per call/session -----------------------
  {
    kind: 'timestamp-datetime',
    severity: 'high',
    pattern: new RegExp(
      `${B_BEFORE}\\d{4}-\\d{2}-\\d{2}[T ]\\d{2}:\\d{2}(?::\\d{2}(?:\\.\\d+)?)?(?:Z|[+-]\\d{2}:?\\d{2})?${B_AFTER}`,
      'gu',
    ),
    why: 'A full datetime changes every call — everything after it can never match byte-for-byte.',
  },
  {
    kind: 'timestamp-epoch',
    severity: 'high',
    pattern: new RegExp(`${B_BEFORE}1[6-9]\\d{8}(?:\\d{3})?${B_AFTER}`, 'gu'),
    why: 'Looks like a Unix epoch timestamp (seconds or ms) — changes every call.',
  },
  {
    kind: 'uuid',
    severity: 'high',
    pattern: new RegExp(
      `${B_BEFORE}[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}${B_AFTER}`,
      'gu',
    ),
    why: 'UUIDs are unique per session/request — a new value on the next call breaks the prefix.',
  },
  {
    kind: 'session-id',
    severity: 'high',
    pattern: new RegExp(
      `${B_BEFORE}(?:session|request|trace|correlation|conversation|run|call|message|user)` +
        `[ _-]?id${B_AFTER}\\s*["':=]+\\s*["']?[A-Za-z0-9._-]{4,}`,
      'giu',
    ),
    why:
      'Per-session/user identifiers vary between calls and users — no cross-request ' +
      '(or cross-user) prefix sharing.',
  },
  {
    kind: 'secret-like',
    severity: 'high',
    pattern: new RegExp(
      `${B_BEFORE}(?:api[ _-]?key|secret|bearer|authorization)${B_AFTER}\\s*[:=]\\s*["']?[A-Za-z0-9._+/-]{12,}`,
      'giu',
    ),
    why:
      'Secret material in the prompt: rotates (breaking the cache) AND is a security smell — ' +
      "keys don't belong in prompts.",
  },

  // --- unrendered template slots (will vary once rendered) ------------------
  {
    kind: 'template-jinja',
    severity: 'medium',
    pattern: /\{\{[^{}]{1,120}\}\}|\{%[^{}]{1,120}%\}/gu,
    why: 'Jinja/Handlebars template slot — after rendering, the value will differ per call.',
  },
  {
    kind: 'template-braces',
    severity: 'medium',
    pattern: /\{[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+|\[[^\]]{0,40}\])*\}/gu,
    why:
      'Python .format()/f-string style placeholder — a per-call value will land here. ' +
      '(If this is literal JSON/code content, ignore.)',
  },
  {
    kind: 'template-dollar',
    severity: 'medium',
    pattern: /\$\{[^}]{1,120}\}/gu,
    why: '${...} template slot (JS template / shell) — value will differ per call.',
  },
  {
    kind: 'template-percent-named',
    severity: 'medium',
    pattern: /%\([A-Za-z_][A-Za-z0-9_]*\)[sdifr]/gu,
    why: '%-style named placeholder — value will differ per call.',
  },
  {
    kind: 'template-angle',
    severity: 'medium',
    pattern: /<[A-Z][A-Z0-9_]{2,29}>/gu,
    why: 'ALL-CAPS angle-bracket placeholder (e.g. <USER_NAME>) — value will differ per call.',
  },
  {
    kind: 'template-bracket',
    severity: 'medium',
    pattern: /\[(?:insert|your|todo|tbd|placeholder|fill[ _-]?in)[^\]]{0,60}\]/giu,
    why: '[INSERT ...]-style placeholder — value will differ per call.',
  },

  // --- date-ish content (changes daily / signals injected 'now') ------------
  {
    kind: 'date-phrase',
    severity: 'medium',
    pattern: new RegExp(
      `${B_BEFORE}(?:today'?s date|the current (?:date|time)|current (?:date|time) is|` +
        `as of (?:today|now)|right now it is|current timestamp)${B_AFTER}`,
      'giu',
    ),
    why:
      "Phrase signaling an injected 'current date/time' — the value next to it changes " +
      'every day or every call.',
  },
  {
    kind: 'date-iso',
    severity: 'medium',
    pattern: new RegExp(`${B_BEFORE}\\d{4}-\\d{2}-\\d{2}${B_AFTER}`, 'gu'),
    why:
      "ISO date — if injected as 'today', it breaks the cache at every day boundary. " +
      '(A static historical date is fine — ignore if so.)',
  },
  {
    kind: 'date-us',
    severity: 'medium',
    pattern: new RegExp(`${B_BEFORE}\\d{1,2}/\\d{1,2}/\\d{4}${B_AFTER}`, 'gu'),
    why: "Date literal — breaks the cache daily if injected as 'today'.",
  },
  {
    kind: 'date-verbose',
    severity: 'medium',
    pattern: new RegExp(`${B_BEFORE}${MONTHS}\\.?\\s+\\d{1,2},?\\s+\\d{4}${B_AFTER}`, 'gu'),
    why: "Spelled-out date — breaks the cache daily if injected as 'today'.",
  },

  // --- advisory-only --------------------------------------------------------
  {
    kind: 'clock-time',
    severity: 'low',
    // SPEC.md delta f: the optional space is consumed only when am/pm follows.
    pattern: new RegExp(`${B_BEFORE}\\d{1,2}:\\d{2}(?::\\d{2})?(?:\\s?[AaPp][Mm])?${B_AFTER}`, 'gu'),
    why: "Clock time — advisory; only a problem if it's an injected 'now'.",
  },
  {
    kind: 'template-percent-bare',
    severity: 'low',
    pattern: new RegExp(`(?<!${NW}|%)%[sdif]${B_AFTER}`, 'gu'),
    why: 'Bare %-placeholder — advisory (often literal text).',
  },
  {
    kind: 'hex-id',
    severity: 'low',
    pattern: new RegExp(`${B_BEFORE}[0-9a-fA-F]{16,64}${B_AFTER}`, 'gu'),
    why: 'Long hex string (object id / hash) — advisory; a problem if it varies per request.',
    validate: hexMixed,
  },
];

const EXCERPT_PAD = 28;

function excerpt(cp: CpIndex, start: number, end: number): string {
  const lo = Math.max(0, start - EXCERPT_PAD);
  const hi = Math.min(cp.length, end + EXCERPT_PAD);
  const snippet = cp.slice(lo, hi).split('\n').join('\\n');
  const prefix = lo > 0 ? '...' : '';
  const suffix = hi < cp.length ? '...' : '';
  return `${prefix}${snippet}${suffix}`;
}

/** Run all detectors; dedupe findings fully contained inside a longer one. */
export function scan(text: string): Finding[] {
  const cp = new CpIndex(text);
  const raw: Finding[] = [];
  for (const det of DETECTORS) {
    det.pattern.lastIndex = 0;
    for (const m of text.matchAll(det.pattern)) {
      const matched = m[0];
      if (det.validate && !det.validate(matched)) continue;
      const start = cp.u16ToCp(m.index as number);
      const end = cp.u16ToCp((m.index as number) + matched.length);
      raw.push({
        kind: det.kind,
        severity: det.severity,
        start,
        end,
        excerpt: excerpt(cp, start, end),
        why: det.why,
        section: '',
        section_offset: 0,
        pct_into_prompt: 0.0,
      });
    }
  }
  // longer spans first at equal start, so contained shorter matches get dropped
  // (stable sort: ties keep detector declaration order, then match order)
  raw.sort((a, b) => a.start - b.start || b.end - b.start - (a.end - a.start));
  const kept: Finding[] = [];
  for (const f of raw) {
    const contained = kept.some(
      (k) => k.start <= f.start && f.end <= k.end && k.end - k.start > f.end - f.start,
    );
    if (!contained) kept.push(f);
  }
  if (text) {
    for (const f of kept) {
      f.pct_into_prompt = (100.0 * f.start) / cp.length;
    }
  }
  return kept;
}

const BLOCKING = new Set(['high', 'medium']);

/** Earliest finding that caps the stable (cacheable) prefix. */
export function firstBlocking(findings: Finding[]): Finding | null {
  let best: Finding | null = null;
  for (const f of findings) {
    if (BLOCKING.has(f.severity) && (best === null || f.start < best.start)) best = f;
  }
  return best;
}

export function blocking(findings: Finding[]): Finding[] {
  return findings.filter((f) => BLOCKING.has(f.severity));
}
