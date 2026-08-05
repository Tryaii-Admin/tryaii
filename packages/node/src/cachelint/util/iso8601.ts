/**
 * Strict ISO-8601 subset parser shared with the Python SDK (SPEC.md §1.6).
 *
 * Both engines parse `sent_at` with the SAME regex grammar and the same
 * integer epoch math — never a platform date parser (Date.parse interprets
 * offset-less timestamps as LOCAL time; Python's fromisoformat accepts a
 * version-dependent dialect). Naive timestamps are UTC; anything malformed or
 * out-of-range parses to null, which disables the TTL check for that pair.
 */

const ISO_RE =
  /^(\d{4})-(\d{2})-(\d{2})([Tt ](\d{2}):(\d{2})(:(\d{2})(\.(\d{1,9}))?)?)?(Z|z|[+-]\d{2}:?\d{2})?$/;

const DAYS_IN_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];

function isLeap(year: number): boolean {
  return year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
}

/**
 * Days since 1970-01-01 (proleptic Gregorian). Howard Hinnant's algorithm —
 * pure integer math, implemented identically in the Python SDK.
 */
function daysFromCivil(year: number, month: number, day: number): number {
  year -= month <= 2 ? 1 : 0;
  const era = Math.floor((year >= 0 ? year : year - 399) / 400);
  const yoe = year - era * 400;
  const doy = Math.floor((153 * (month + (month > 2 ? -3 : 9)) + 2) / 5) + day - 1;
  const doe = yoe * 365 + Math.floor(yoe / 4) - Math.floor(yoe / 100) + doy;
  return era * 146097 + doe - 719468;
}

/** Parse a sent_at string to epoch milliseconds, or null if invalid. */
export function parseTsMs(value: unknown): number | null {
  if (!value || typeof value !== 'string') return null;
  const m = ISO_RE.exec(value);
  if (m === null) return null;
  const year = Number(m[1]);
  const month = Number(m[2]);
  const day = Number(m[3]);
  if (month < 1 || month > 12) return null;
  const maxDay = DAYS_IN_MONTH[month - 1] + (month === 2 && isLeap(year) ? 1 : 0);
  if (day < 1 || day > maxDay) return null;

  let hour = 0;
  let minute = 0;
  let second = 0;
  let ms = 0;
  if (m[4]) {
    hour = Number(m[5]);
    minute = Number(m[6]);
    if (m[8]) second = Number(m[8]);
    if (m[10]) ms = Number(m[10].slice(0, 3).padEnd(3, '0')); // truncate to ms
    if (hour > 23 || minute > 59 || second > 59) return null;
  }

  let offsetMin = 0;
  const tz = m[11];
  if (tz && tz !== 'Z' && tz !== 'z') {
    const sign = tz[0] === '-' ? -1 : 1;
    const digits = tz.slice(1).replace(':', '');
    const offH = Number(digits.slice(0, 2));
    const offM = Number(digits.slice(2));
    if (offH > 23 || offM > 59) return null;
    offsetMin = sign * (offH * 60 + offM);
  }

  const days = daysFromCivil(year, month, day);
  const totalMs = (((days * 24 + hour) * 60 + minute) * 60 + second) * 1000 + ms;
  return totalMs - offsetMin * 60 * 1000;
}
