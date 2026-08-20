/**
 * Unicode code-point indexing (SPEC.md §1.1).
 *
 * Every offset the cachelint engine reports — finding spans, section spans,
 * LCPs, excerpt windows — is measured in CODE POINTS, matching Python's str
 * indexing. JS strings and regex match indices are UTF-16 code units, so any
 * string containing astral characters (emoji etc.) needs conversion.
 */

/** Number of Unicode code points in the string (Python len()). */
export function cpLength(s: string): number {
  let n = 0;
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c >= 0xd800 && c <= 0xdbff && i + 1 < s.length) {
      const d = s.charCodeAt(i + 1);
      if (d >= 0xdc00 && d <= 0xdfff) i++;
    }
    n++;
  }
  return n;
}

/**
 * Bidirectional UTF-16 <-> code-point index map for one string, plus
 * code-point slicing. Built once per scanned text; O(1) lookups.
 */
export class CpIndex {
  private readonly text: string;
  /** cpToU16[cp] = UTF-16 index where code point `cp` starts; length = cpCount + 1. */
  private readonly cpToU16: number[];
  /** u16ToCpArr[u16] = code-point index containing that UTF-16 unit; length = s.length + 1. */
  private readonly u16ToCpArr: number[];

  constructor(s: string) {
    this.text = s;
    this.cpToU16 = [];
    this.u16ToCpArr = new Array(s.length + 1);
    let cp = 0;
    let i = 0;
    while (i < s.length) {
      this.cpToU16.push(i);
      this.u16ToCpArr[i] = cp;
      const c = s.charCodeAt(i);
      let width = 1;
      if (c >= 0xd800 && c <= 0xdbff && i + 1 < s.length) {
        const d = s.charCodeAt(i + 1);
        if (d >= 0xdc00 && d <= 0xdfff) width = 2;
      }
      if (width === 2) this.u16ToCpArr[i + 1] = cp;
      i += width;
      cp++;
    }
    this.cpToU16.push(s.length);
    this.u16ToCpArr[s.length] = cp;
  }

  /** Code-point count (Python len()). */
  get length(): number {
    return this.cpToU16.length - 1;
  }

  /** Convert a UTF-16 index (e.g. a regex match index) to a code-point index. */
  u16ToCp(u16: number): number {
    if (u16 <= 0) return 0;
    if (u16 >= this.u16ToCpArr.length) return this.length;
    return this.u16ToCpArr[u16];
  }

  /** UTF-16 index where code point `cp` starts. */
  cpToU16Index(cp: number): number {
    if (cp <= 0) return 0;
    if (cp >= this.cpToU16.length) return this.text.length;
    return this.cpToU16[cp];
  }

  /** Python-style slice by code-point offsets. */
  slice(startCp: number, endCp?: number): string {
    const end = endCp === undefined ? this.length : endCp;
    return this.text.slice(this.cpToU16Index(Math.max(0, startCp)),
                           this.cpToU16Index(Math.max(0, end)));
  }
}

/** Longest common prefix of two strings, in CODE POINTS (never splits a pair). */
export function cpLcp(a: string, b: string): number {
  let i = 0; // UTF-16 cursor
  let cp = 0;
  const n = Math.min(a.length, b.length);
  while (i < n) {
    const ca = a.charCodeAt(i);
    if (ca !== b.charCodeAt(i)) break;
    let width = 1;
    if (ca >= 0xd800 && ca <= 0xdbff && i + 1 < n) {
      // Both strings agree on this high surrogate; the pair must match fully
      // to count — comparing unit-by-unit handles that on the next check.
      const da = a.charCodeAt(i + 1);
      if (da >= 0xdc00 && da <= 0xdfff) {
        if (da !== b.charCodeAt(i + 1)) break; // split pair: stop BEFORE it
        width = 2;
      }
    }
    i += width;
    cp++;
  }
  return cp;
}
