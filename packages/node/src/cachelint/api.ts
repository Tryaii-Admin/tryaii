/** Top-level API: normalize input(s), run per-item + sequence analysis. */

import { analyzeItem, type AnalyzedItem } from './analyzer.js';
import { META } from './providers.js';
import { analyzeSequences } from './sequence.js';

/** Accept: a single item, a list of items, or {"inputs": [...]}. */
function normalizeInput(data: unknown): Record<string, unknown>[] {
  let d = data;
  if (d !== null && typeof d === 'object' && !Array.isArray(d) && 'inputs' in d) {
    d = (d as Record<string, unknown>).inputs;
  }
  if (d !== null && typeof d === 'object' && !Array.isArray(d)) {
    d = [d];
  }
  if (!Array.isArray(d)) {
    throw new Error('Input must be an object, a list of objects, or {"inputs": [...]}');
  }
  const items: Record<string, unknown>[] = [];
  for (let i = 0; i < d.length; i++) {
    const raw = d[i];
    if (typeof raw === 'string') {
      throw new Error(
        `input #${i} is a bare string — wrap it: ` +
          '{"prompt": "...", "llm": {"provider": "...", "name": "..."}}',
      );
    }
    if (raw === null || typeof raw !== 'object' || Array.isArray(raw) || !('prompt' in raw)) {
      throw new Error(`input #${i}: expected an object with a 'prompt' field`);
    }
    items.push(raw as Record<string, unknown>);
  }
  return items;
}

/**
 * Analyze one or more {prompt, llm:{provider,name}, sent_at?} inputs.
 *
 * Returns {items, sequences, meta} — sequences only when the list has 2+
 * entries. Numbers are JS-native, which already satisfies the SPEC.md §1.3
 * integer rule the Python engine normalizes toward.
 */
export function analyze(data: unknown): Record<string, unknown> {
  const rawItems = normalizeInput(data);
  const analyzed: AnalyzedItem[] = rawItems.map((item, i) => analyzeItem(item, i));
  return {
    items: analyzed.map((a) => a.data),
    sequences: analyzed.length > 1 ? analyzeSequences(analyzed) : [],
    meta: { ...META },
  };
}

/**
 * Like analyze(), but each item also carries the canonical text and section
 * spans — the shape a renderer that marks findings inline consumes (it needs
 * character offsets).
 */
export function analyzeFull(data: unknown): Record<string, unknown> {
  const rawItems = normalizeInput(data);
  const analyzed: AnalyzedItem[] = rawItems.map((item, i) => analyzeItem(item, i));
  return {
    items: analyzed.map((a) => ({
      data: a.data,
      canonical: a.canonical,
      sections: a.sections.map((s) => ({ name: s.name, start: s.start, end: s.end })),
      sent_at: a.sent_at,
    })),
    sequences: analyzed.length > 1 ? analyzeSequences(analyzed) : [],
    meta: { ...META },
  };
}
