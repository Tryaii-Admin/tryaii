/**
 * Round-half-even matching Python's round() / format(x, '.Nf') semantics
 * (SPEC.md §1.2). Engine code must never use Math.round — it rounds halves
 * toward +Infinity, Python rounds them to the nearest EVEN digit, and token
 * counts sit directly on verdict boundaries.
 *
 * Python rounds the exact BINARY value of the double (so round(2.675, 2) is
 * 2.67 — the stored value is 2.67499...). We reproduce that by expanding the
 * double to a correctly-rounded 20-decimal string (Number#toFixed is spec'd
 * correctly rounded) and doing decimal half-even rounding on the digits.
 */

export function halfEvenRound(x: number, digits = 0): number {
  if (!Number.isFinite(x)) return x;
  const negative = x < 0;
  const abs = Math.abs(x);
  if (abs >= 1e15) return x; // beyond exact-integer doubles; out of engine domain

  const fixed = abs.toFixed(20); // "240.25000000000000000000"
  const dot = fixed.indexOf('.');
  const intPart = fixed.slice(0, dot);
  const decPart = fixed.slice(dot + 1);

  const kept = decPart.slice(0, digits);
  const rest = decPart.slice(digits);
  const tie = '5' + '0'.repeat(rest.length - 1);

  let digitsStr = intPart + kept; // the number scaled by 10^digits, as digits
  let roundUp: boolean;
  if (rest > tie) roundUp = true;
  else if (rest < tie) roundUp = false;
  else {
    const last = digitsStr.charCodeAt(digitsStr.length - 1) - 48;
    roundUp = last % 2 === 1; // exact tie: round to even
  }
  if (roundUp) digitsStr = (BigInt(digitsStr) + 1n).toString();

  let result: number;
  if (digits === 0) {
    result = Number(digitsStr);
  } else {
    const padded = digitsStr.padStart(digits + 1, '0');
    result = Number(padded.slice(0, -digits) + '.' + padded.slice(-digits));
  }
  return negative ? -result : result;
}

/** Python f"{x:.Nf}" — fixed-point formatting with half-even rounding. */
export function formatFixed(x: number, digits: number): string {
  return halfEvenRound(x, digits).toFixed(digits);
}
