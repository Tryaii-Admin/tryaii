"""Cross-language JSON helpers (SPEC.md §1.3, §1.4).

The Python `json.dumps` / JS `JSON.stringify` byte-parity contract rests on
two normalizations applied here:

  * integral floats become integers (1.0 -> 1, 100.0 -> 100) — JSON.parse
    cannot even represent the difference, so Python must not either;
  * the canonical `tools` rendering is minified with recursively sorted keys.
"""

from __future__ import annotations

import json
from typing import Any


def normalize_numbers(obj: Any) -> Any:
    """Return a copy of a JSON-shaped tree with integral floats as ints."""
    if isinstance(obj, bool):          # bool is an int subclass — leave it alone
        return obj
    if isinstance(obj, float):
        return int(obj) if obj.is_integer() else obj
    if isinstance(obj, dict):
        return {k: normalize_numbers(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [normalize_numbers(v) for v in obj]
    return obj


def sorted_minified_dumps(obj: Any) -> str:
    """Minified JSON with recursively sorted keys and integral floats as ints.

    Byte-identical to the Node SDK's sortedStringify (SPEC.md §1.4): keys sort
    by code-unit order, separators are bare , and :, non-ASCII stays verbatim.
    """
    return json.dumps(normalize_numbers(obj), sort_keys=True,
                      ensure_ascii=False, separators=(",", ":"))
