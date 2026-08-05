/**
 * Canonical JSON serialization for the tools section (SPEC.md §1.4):
 * minified, keys recursively sorted by code-point order, byte-identical to
 * Python's json.dumps(sort_keys=True, ensure_ascii=False,
 * separators=(",", ":")) after integral-float normalization.
 *
 * In JS integral floats ARE integers (JSON.parse cannot represent 1.0), so
 * the §1.3 normalization is native here; the Python engine applies it
 * explicitly to match.
 */

function codePointCompare(a: string, b: string): number {
  // Python sorts str by code point; JS default sort is by UTF-16 code unit,
  // which disagrees above U+FFFF. Compare code points explicitly.
  const ai = a[Symbol.iterator]();
  const bi = b[Symbol.iterator]();
  for (;;) {
    const av = ai.next();
    const bv = bi.next();
    if (av.done && bv.done) return 0;
    if (av.done) return -1;
    if (bv.done) return 1;
    const ac = av.value.codePointAt(0) as number;
    const bc = bv.value.codePointAt(0) as number;
    if (ac !== bc) return ac - bc;
  }
}

export function sortedStringify(value: unknown): string {
  if (value === null || typeof value !== 'object') {
    // -0 stringifies as "0" (Python emits "0" too after normalize_numbers).
    return JSON.stringify(value) ?? 'null';
  }
  if (Array.isArray(value)) {
    return '[' + value.map((v) => (v === undefined ? 'null' : sortedStringify(v))).join(',') + ']';
  }
  const obj = value as Record<string, unknown>;
  const keys = Object.keys(obj)
    .filter((k) => obj[k] !== undefined)
    .sort(codePointCompare);
  const parts = keys.map((k) => JSON.stringify(k) + ':' + sortedStringify(obj[k]));
  return '{' + parts.join(',') + '}';
}
