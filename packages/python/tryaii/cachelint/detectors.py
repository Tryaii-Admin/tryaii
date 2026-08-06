"""Dynamic-content detectors (18 kinds).

Prompt caching is a PREFIX match: the first byte that changes between requests
invalidates everything after it. These detectors flag content that changes (or
will change, once a template is rendered) between calls — each finding carries
its offset so the analyzer can compute the surviving stable prefix.

Severity semantics:
  high   — value certainly varies per call/session (datetime, UUID, session id, epoch)
  medium — likely varies between calls (unrendered template slot, date, injected-date phrase)
  low    — suspicious but often static content (bare clock times, %s, hex ids)

high + medium block the stable prefix; low is advisory only.

The regexes are the semantic reference for the Node SDK's detectors.ts
(SPEC.md §1.7): Python's Unicode-aware \\b translates there to explicit
\\p{L}/\\p{N} lookarounds. Offsets are Unicode code points (SPEC.md §1.1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class Detector:
    kind: str
    severity: str
    pattern: re.Pattern
    why: str
    validate: Optional[Callable[[str], bool]] = None  # extra post-filter on the match text


@dataclass
class Finding:
    kind: str
    severity: str
    start: int
    end: int
    excerpt: str
    why: str
    section: str = ""
    section_offset: int = 0
    pct_into_prompt: float = 0.0

    def to_dict(self) -> dict:
        pct = round(self.pct_into_prompt, 1)
        if isinstance(pct, float) and pct.is_integer():   # SPEC.md §1.3 integer rule
            pct = int(pct)
        return {
            "kind": self.kind, "severity": self.severity,
            "start": self.start, "end": self.end,
            "section": self.section, "section_offset": self.section_offset,
            "pct_into_prompt": pct,
            "excerpt": self.excerpt, "why": self.why,
        }


def _hex_mixed(s: str) -> bool:
    """Require both digits and letters so plain numbers / words don't fire."""
    return any(c.isdigit() for c in s) and any(c.isalpha() for c in s)


_MONTHS = (r"(?:January|February|March|April|May|June|July|August|September|"
           r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)")

DETECTORS: tuple[Detector, ...] = (
    # --- values that certainly change per call/session -----------------------
    Detector(
        kind="timestamp-datetime", severity="high",
        pattern=re.compile(
            r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?\b"),
        why="A full datetime changes every call — everything after it can never match byte-for-byte.",
    ),
    Detector(
        kind="timestamp-epoch", severity="high",
        pattern=re.compile(r"\b1[6-9]\d{8}(?:\d{3})?\b"),
        why="Looks like a Unix epoch timestamp (seconds or ms) — changes every call.",
    ),
    Detector(
        kind="uuid", severity="high",
        pattern=re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
        why="UUIDs are unique per session/request — a new value on the next call breaks the prefix.",
    ),
    Detector(
        kind="session-id", severity="high",
        pattern=re.compile(
            r"(?i)\b(?:session|request|trace|correlation|conversation|run|call|message|user)"
            r"[ _-]?id\b\s*[\"':=]+\s*[\"']?[A-Za-z0-9._-]{4,}"),
        why="Per-session/user identifiers vary between calls and users — no cross-request "
            "(or cross-user) prefix sharing.",
    ),
    Detector(
        kind="secret-like", severity="high",
        pattern=re.compile(
            r"(?i)\b(?:api[ _-]?key|secret|bearer|authorization)\b\s*[:=]\s*[\"']?[A-Za-z0-9._+/-]{12,}"),
        why="Secret material in the prompt: rotates (breaking the cache) AND is a security smell — "
            "keys don't belong in prompts.",
    ),

    # --- unrendered template slots (will vary once rendered) ------------------
    Detector(
        kind="template-jinja", severity="medium",
        pattern=re.compile(r"\{\{[^{}]{1,120}\}\}|\{%[^{}]{1,120}%\}"),
        why="Jinja/Handlebars template slot — after rendering, the value will differ per call.",
    ),
    Detector(
        kind="template-braces", severity="medium",
        pattern=re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+|\[[^\]]{0,40}\])*\}"),
        why="Python .format()/f-string style placeholder — a per-call value will land here. "
            "(If this is literal JSON/code content, ignore.)",
    ),
    Detector(
        kind="template-dollar", severity="medium",
        pattern=re.compile(r"\$\{[^}]{1,120}\}"),
        why="${...} template slot (JS template / shell) — value will differ per call.",
    ),
    Detector(
        kind="template-percent-named", severity="medium",
        pattern=re.compile(r"%\([A-Za-z_][A-Za-z0-9_]*\)[sdifr]"),
        why="%-style named placeholder — value will differ per call.",
    ),
    Detector(
        kind="template-angle", severity="medium",
        pattern=re.compile(r"<[A-Z][A-Z0-9_]{2,29}>"),
        why="ALL-CAPS angle-bracket placeholder (e.g. <USER_NAME>) — value will differ per call.",
    ),
    Detector(
        kind="template-bracket", severity="medium",
        pattern=re.compile(r"(?i)\[(?:insert|your|todo|tbd|placeholder|fill[ _-]?in)[^\]]{0,60}\]"),
        why="[INSERT ...]-style placeholder — value will differ per call.",
    ),

    # --- date-ish content (changes daily / signals injected 'now') ------------
    Detector(
        kind="date-phrase", severity="medium",
        pattern=re.compile(
            r"(?i)\b(?:today'?s date|the current (?:date|time)|current (?:date|time) is|"
            r"as of (?:today|now)|right now it is|current timestamp)\b"),
        why="Phrase signaling an injected 'current date/time' — the value next to it changes "
            "every day or every call.",
    ),
    Detector(
        kind="date-iso", severity="medium",
        pattern=re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
        why="ISO date — if injected as 'today', it breaks the cache at every day boundary. "
            "(A static historical date is fine — ignore if so.)",
    ),
    Detector(
        kind="date-us", severity="medium",
        pattern=re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b"),
        why="Date literal — breaks the cache daily if injected as 'today'.",
    ),
    Detector(
        kind="date-verbose", severity="medium",
        pattern=re.compile(r"\b" + _MONTHS + r"\.?\s+\d{1,2},?\s+\d{4}\b"),
        why="Spelled-out date — breaks the cache daily if injected as 'today'.",
    ),

    # --- advisory-only --------------------------------------------------------
    Detector(
        kind="clock-time", severity="low",
        # SPEC.md delta f: the optional space is consumed only when am/pm follows.
        pattern=re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?(?:\s?[AaPp][Mm])?\b"),
        why="Clock time — advisory; only a problem if it's an injected 'now'.",
    ),
    Detector(
        kind="template-percent-bare", severity="low",
        pattern=re.compile(r"(?<![\w%])%[sdif]\b"),
        why="Bare %-placeholder — advisory (often literal text).",
    ),
    Detector(
        kind="hex-id", severity="low",
        pattern=re.compile(r"\b[0-9a-fA-F]{16,64}\b"),
        why="Long hex string (object id / hash) — advisory; a problem if it varies per request.",
        validate=_hex_mixed,
    ),
)

_EXCERPT_PAD = 28


def _excerpt(text: str, start: int, end: int) -> str:
    lo = max(0, start - _EXCERPT_PAD)
    hi = min(len(text), end + _EXCERPT_PAD)
    snippet = text[lo:hi].replace("\n", "\\n")
    prefix = "..." if lo > 0 else ""
    suffix = "..." if hi < len(text) else ""
    return f"{prefix}{snippet}{suffix}"


def scan(text: str) -> list[Finding]:
    """Run all detectors; dedupe findings fully contained inside a longer one."""
    raw: list[Finding] = []
    for det in DETECTORS:
        for m in det.pattern.finditer(text):
            if det.validate is not None and not det.validate(m.group(0)):
                continue
            raw.append(Finding(
                kind=det.kind, severity=det.severity,
                start=m.start(), end=m.end(),
                excerpt=_excerpt(text, m.start(), m.end()),
                why=det.why,
            ))
    # longer spans first at equal start, so contained shorter matches get dropped
    raw.sort(key=lambda f: (f.start, -(f.end - f.start)))
    kept: list[Finding] = []
    for f in raw:
        contained = any(
            k.start <= f.start and f.end <= k.end and (k.end - k.start) > (f.end - f.start)
            for k in kept
        )
        if not contained:
            kept.append(f)
    if text:
        for f in kept:
            f.pct_into_prompt = 100.0 * f.start / len(text)
    return kept


_BLOCKING = ("high", "medium")


def first_blocking(findings: list[Finding]) -> Optional[Finding]:
    """Earliest finding that caps the stable (cacheable) prefix."""
    blockers = [f for f in findings if f.severity in _BLOCKING]
    return min(blockers, key=lambda f: f.start) if blockers else None


def blocking(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity in _BLOCKING]
