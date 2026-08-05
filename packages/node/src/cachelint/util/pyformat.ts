/**
 * Locale-independent formatting helpers matching Python f-string output
 * (SPEC.md §1.5). Never use toLocaleString — the report is byte-compared
 * across machines and locales.
 */

/** Python f"{n:,}" for integers — comma thousands separators. */
export function formatThousands(n: number): string {
  const negative = n < 0;
  const digits = Math.trunc(Math.abs(n)).toString();
  let out = '';
  for (let i = 0; i < digits.length; i++) {
    if (i > 0 && (digits.length - i) % 3 === 0) out += ',';
    out += digits[i];
  }
  return negative ? '-' + out : out;
}
