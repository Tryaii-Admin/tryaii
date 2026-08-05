/**
 * Token counting per provider — exact-required policy (SPEC.md §3).
 *
 * - OpenAI / xAI / *-with-openai-upstream: exact via js-tiktoken o200k_base
 *   (a hard dependency of the package; the rank data loads lazily on first
 *   use and only this module imports it).
 * - Anthropic: heuristic estimate (~3.5 chars/token English). tiktoken is
 *   WRONG for Claude — for exact counts use POST /v1/messages/count_tokens.
 * - Gemini / Vertex / Bedrock / others: heuristic estimate.
 *
 * Lengths are Unicode code points (SPEC.md §1.1); rounding is half-even
 * (SPEC.md §1.2); the method label reports the rate actually used after the
 * non-ASCII adjustment (SPEC.md delta k). Byte-identical labels/notes to the
 * Python reference.
 */

import { Tiktoken } from 'js-tiktoken/lite';
import o200kBase from 'js-tiktoken/ranks/o200k_base';

import { halfEvenRound } from './util/halfEven.js';

export interface TokenCount {
  tokens: number;
  method: string; // "tiktoken(o200k_base)" | "estimate(~N chars/tok)" | "empty"
  exact: boolean;
  note: string;
}

let tiktokenEnc: Tiktoken | null = null;

function getTiktoken(): Tiktoken {
  if (tiktokenEnc === null) {
    tiktokenEnc = new Tiktoken(o200kBase);
  }
  return tiktokenEnc;
}

// chars-per-token rates for the heuristic path (latin-heavy text)
const RATES: Record<string, number> = {
  anthropic: 3.5,
  gemini: 4.0,
  vertex: 4.0,
  openai: 4.0,
  xai: 3.8,
  openrouter: 4.0,
  bedrock: 3.7,
};

const EXACT_NOTES: Record<string, string> = {
  anthropic: 'for exact counts use POST /v1/messages/count_tokens (never tiktoken for Claude)',
  gemini: 'for exact counts use the Gemini countTokens API',
  vertex: 'for exact counts use the Vertex countTokens API / Anthropic count_tokens',
  bedrock: "for exact counts use the underlying vendor's token-counting API",
  xai: 'xAI tokenizer approximated with o200k_base',
  openrouter: "count depends on the upstream model's tokenizer",
  openai: '',
};

// Providers whose tokenizer tiktoken o200k_base approximates well enough to call exact-ish.
const TIKTOKEN_OK = new Set(['openai']);
const TIKTOKEN_APPROX = new Set(['xai']); // close, but not official — label as approximate

const NON_ASCII_SWITCH = 0.3;
const NON_ASCII_RATE = 2.6;

/** Heuristic count; returns [tokens, effectiveRate] — SPEC.md delta k. */
function estimate(text: string, rate: number): [number, number] {
  if (!text) return [0, rate];
  // Non-latin scripts (Hebrew, CJK, ...) tokenize denser per char.
  // Iterate CODE POINTS (an astral emoji is one "char" like in Python).
  let nonAscii = 0;
  let cpLen = 0;
  for (const ch of text) {
    if ((ch.codePointAt(0) as number) > 127) nonAscii++;
    cpLen++;
  }
  if (nonAscii / Math.max(1, cpLen) > NON_ASCII_SWITCH) {
    rate = Math.min(rate, NON_ASCII_RATE);
  }
  return [Math.max(1, halfEvenRound(cpLen / rate)), rate];
}

/**
 * Count/estimate tokens for `text` under the given provider.
 * `upstream` (for openrouter) refines the choice of tokenizer.
 */
export function countTokens(
  text: string,
  providerKey: string,
  upstream: string | null = null,
): TokenCount {
  const key = providerKey;
  const effective = upstream || key;
  if (!text) {
    return { tokens: 0, method: 'empty', exact: true, note: '' };
  }

  if (TIKTOKEN_OK.has(effective) || TIKTOKEN_APPROX.has(effective)) {
    const enc = getTiktoken();
    // Special tokens encode as ordinary text (mirrors Python disallowed_special=()).
    const n = enc.encode(text, [], []).length;
    const exact = TIKTOKEN_OK.has(effective);
    const note = exact ? '' : EXACT_NOTES[effective] ?? 'approximated with o200k_base';
    return { tokens: n, method: 'tiktoken(o200k_base)', exact, note };
  }

  const rate = RATES[effective] ?? RATES[key] ?? 4.0;
  const [n, usedRate] = estimate(text, rate);
  return {
    tokens: n,
    method: `estimate(~${usedRate} chars/tok)`,
    exact: false,
    note: (EXACT_NOTES[effective] ?? EXACT_NOTES[key] ?? '') || 'heuristic estimate (+/-20%)',
  };
}
