"""hygiene check (SPEC.md §2.5) — provider-independent prompt structure scan."""

from __future__ import annotations

from typing import Any

from tryaii.cachelint.analyzer import hygiene_findings

FINDINGS_EMITTED = 10
_FIELDS = ("kind", "severity", "section", "section_offset",
           "pct_into_prompt", "excerpt", "why", "hint")

NO_SYSTEM_ADVISORY = ("No system block: shared static instructions in a system "
                      "block form the classic cacheable unit.")


def run_hygiene(prompt: Any) -> dict:
    h = hygiene_findings(prompt)

    advisories: list[str] = []
    structured = isinstance(prompt, dict)
    if structured and not h["has_system"] and not h["has_tools"] \
            and h["message_count"] >= 2:
        advisories.append(NO_SYSTEM_ADVISORY)
    if h["findings_in_system"] > 0:
        advisories.append(
            f"{h['findings_in_system']} dynamic value(s) inside the system "
            "block — the system block should be fully static.")

    return {
        "status": "finding" if h["blocking_count"] > 0 else "ok",
        "reason": None,
        "findings_count": len(h["findings"]),
        "blocking_count": h["blocking_count"],
        "findings_in_system": h["findings_in_system"],
        "findings": [
            {k: f[k] for k in _FIELDS} for f in h["findings"][:FINDINGS_EMITTED]
        ],
        "advisories": advisories,
    }
