"""Strict ISO-8601 subset parser shared with the Node SDK (SPEC.md §1.6).

Both engines parse `sent_at` with the SAME regex grammar and the same integer
epoch math — never a platform date parser (`datetime.fromisoformat` and JS
`Date.parse` both have version- or locale-dependent behavior). Naive
timestamps are interpreted as UTC; anything malformed or out-of-range parses
to None, which disables the TTL check for that pair.
"""

from __future__ import annotations

import re
from typing import Optional

_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})"
    r"([Tt ](\d{2}):(\d{2})(:(\d{2})(\.(\d{1,9}))?)?)?"
    r"(Z|z|[+-]\d{2}:?\d{2})?$"
)

_DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _days_from_civil(year: int, month: int, day: int) -> int:
    """Days since 1970-01-01 (proleptic Gregorian). Howard Hinnant's algorithm —
    pure integer math, implemented identically in the Node SDK."""
    year -= month <= 2
    era = (year if year >= 0 else year - 399) // 400
    yoe = year - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def parse_ts_ms(value: Optional[str]) -> Optional[int]:
    """Parse a sent_at string to epoch milliseconds, or None if invalid."""
    if not value or not isinstance(value, str):
        return None
    m = _ISO_RE.match(value)
    if m is None:
        return None
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not 1 <= month <= 12:
        return None
    max_day = _DAYS_IN_MONTH[month - 1] + (1 if month == 2 and _is_leap(year) else 0)
    if not 1 <= day <= max_day:
        return None

    hour = minute = second = ms = 0
    if m.group(4):
        hour, minute = int(m.group(5)), int(m.group(6))
        if m.group(8):
            second = int(m.group(8))
        if m.group(10):
            ms = int(m.group(10)[:3].ljust(3, "0"))   # truncate to milliseconds
        if hour > 23 or minute > 59 or second > 59:
            return None

    offset_min = 0
    tz = m.group(11)
    if tz and tz not in ("Z", "z"):
        sign = -1 if tz[0] == "-" else 1
        digits = tz[1:].replace(":", "")
        off_h, off_m = int(digits[:2]), int(digits[2:])
        if off_h > 23 or off_m > 59:
            return None
        offset_min = sign * (off_h * 60 + off_m)

    days = _days_from_civil(year, month, day)
    total_ms = (((days * 24 + hour) * 60 + minute) * 60 + second) * 1000 + ms
    return total_ms - offset_min * 60 * 1000
