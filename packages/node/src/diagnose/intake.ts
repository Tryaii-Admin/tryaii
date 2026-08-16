/**
 * Lenient inventory intake (shared/diagnose/SPEC.md §2.0).
 *
 * Accepts loosely-shaped agent-written inventories and normalizes them
 * without ever guessing: a malformed site is skipped with a reason; a
 * malformed optional field is simply absent. Mirrors the Python reference
 * (diagnose/intake.py) byte-for-byte in every emitted value.
 */

export const DEFAULT_OUTPUT_TOKENS = 500;

const INT_RE = /^-?\d+$/;
const NUM_RE = /^-?\d+(\.\d+)?$/;

export interface NormalizedSite {
  site_id: string;
  index: number;
  file: string;
  line: number;
  prompt: unknown;
  provider: string | null;
  model: string | null;
  calls_per_day: number | null;
  output_tokens: number | null;
  notes: string | null;
  classification: Record<string, unknown> | null;
}

export interface NormalizedInventory {
  defaults: { calls_per_day: number | null; output_tokens: number };
  sites: NormalizedSite[];
  skipped: { index: number; reason: string }[];
}

/** int >= minimum from an int, integral float, or integer string. */
function coerceInt(value: unknown, minimum: number): number | null {
  let n: number;
  if (typeof value === 'number' && Number.isFinite(value) && Number.isInteger(value)) {
    n = value;
  } else if (typeof value === 'string' && INT_RE.test(value.trim())) {
    n = parseInt(value.trim(), 10);
  } else {
    return null;
  }
  return n >= minimum ? n : null;
}

/** Finite number > 0 from a number or numeric string. */
function coercePositiveNumber(value: unknown): number | null {
  let n: number;
  if (typeof value === 'number') {
    n = value;
  } else if (typeof value === 'string' && NUM_RE.test(value.trim())) {
    n = parseFloat(value.trim());
  } else {
    return null;
  }
  return Number.isFinite(n) && n > 0 ? n : null;
}

function cleanStr(value: unknown): string | null {
  if (typeof value === 'string' && value.trim()) return value.trim();
  return null;
}

/** A prompt is a string or a cachelint prompt object; wrong types are absent. */
function cleanPrompt(value: unknown): unknown {
  if (typeof value === 'string' && value) return value;
  if (value !== null && typeof value === 'object' && !Array.isArray(value)) return value;
  return null;
}

/** Validate the `_classification` injection seam; invalid -> absent. */
export function cleanClassification(value: unknown): Record<string, unknown> | null {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return null;
  const v = value as Record<string, unknown>;
  const sims = v.benchmark_similarities;
  if (
    sims === null ||
    typeof sims !== 'object' ||
    Array.isArray(sims) ||
    Object.keys(sims as object).length === 0
  ) {
    return null;
  }
  const cleanSims: Record<string, number> = {};
  for (const [name, score] of Object.entries(sims as Record<string, unknown>)) {
    if (typeof score !== 'number') return null;
    cleanSims[name] = score;
  }
  const out: Record<string, unknown> = { benchmark_similarities: cleanSims };
  for (const key of ['broad_category', 'subcategory'] as const) {
    const s = cleanStr(v[key]);
    if (s !== null) out[key] = s;
  }
  for (const key of ['confidence', 'difficulty'] as const) {
    const n = v[key];
    if (typeof n === 'number') out[key] = n;
  }
  return out;
}

/**
 * Normalize an inventory (SPEC.md §2.0). Throws only when the top-level
 * shape is not an inventory.
 */
export function normalizeInventory(data: unknown): NormalizedInventory {
  let rawSites: unknown[];
  let rawDefaults: Record<string, unknown>;
  if (Array.isArray(data)) {
    rawSites = data;
    rawDefaults = {};
  } else if (
    data !== null &&
    typeof data === 'object' &&
    Array.isArray((data as Record<string, unknown>).sites)
  ) {
    const d = data as Record<string, unknown>;
    rawSites = d.sites as unknown[];
    rawDefaults =
      d.defaults !== null && typeof d.defaults === 'object' && !Array.isArray(d.defaults)
        ? (d.defaults as Record<string, unknown>)
        : {};
  } else {
    throw new Error("inventory must be an object with a 'sites' array, or an array of sites");
  }

  const defaults = {
    calls_per_day: coercePositiveNumber(rawDefaults.calls_per_day),
    output_tokens: coerceInt(rawDefaults.output_tokens, 1) ?? DEFAULT_OUTPUT_TOKENS,
  };

  const sites: NormalizedSite[] = [];
  const skipped: { index: number; reason: string }[] = [];
  const seenIds = new Map<string, number>();

  for (let index = 0; index < rawSites.length; index++) {
    const raw = rawSites[index];
    if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
      skipped.push({ index, reason: 'site is not an object' });
      continue;
    }
    const site = raw as Record<string, unknown>;
    const file = cleanStr(site.file);
    if (file === null) {
      skipped.push({ index, reason: 'missing file' });
      continue;
    }
    const line = coerceInt(site.line, 1);
    if (line === null) {
      skipped.push({ index, reason: 'missing or invalid line' });
      continue;
    }

    let siteId = cleanStr(site.id) ?? `${file}:${line}`;
    const count = (seenIds.get(siteId) ?? 0) + 1;
    seenIds.set(siteId, count);
    if (count > 1) siteId = `${siteId}#${count}`;

    sites.push({
      site_id: siteId,
      index,
      file,
      line,
      prompt: cleanPrompt(site.prompt),
      provider: cleanStr(site.provider),
      model: cleanStr(site.model),
      calls_per_day: coercePositiveNumber(site.calls_per_day),
      output_tokens: coerceInt(site.output_tokens, 1),
      notes: cleanStr(site.notes),
      classification: cleanClassification(site._classification),
    });
  }

  return { defaults, sites, skipped };
}
