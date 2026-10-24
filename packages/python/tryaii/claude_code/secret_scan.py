"""Does a task text look like it holds a secret? (``tryaii claude-code analyze``)

A task text that matches any pattern below is skipped: it is never embedded or
classified, only counted (shared/monitor/SPEC.md §10.7). The patterns err on
the side of skipping. The generic ``name = value`` rule is the cache linter's
``secret-like`` detector (``tryaii.cachelint.detectors``); the rest are
well-known key and token shapes, private-key blocks and password assignments.
"""

from __future__ import annotations

import re

from tryaii.cachelint.detectors import DETECTORS

_CACHELINT_SECRET = next(d.pattern for d in DETECTORS if d.kind == "secret-like")

SECRET_PATTERNS: tuple[re.Pattern, ...] = (
    _CACHELINT_SECRET,
    # PEM / OpenSSH private keys
    re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
    # password / passwd / pwd / token assignments
    re.compile(r"(?i)\b(?:password|passwd|pwd|passphrase|access[_-]?token|refresh[_-]?token|"
               r"client[_-]?secret|auth[_-]?token)\b\s*[:=]\s*[\"']?\S{6,}"),
    # provider keys: OpenAI / Anthropic / OpenRouter (sk-...), TryAii app keys
    re.compile(r"\bsk-(?:ant-|or-|proj-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"\btryaii_[a-z0-9]{6,}_[A-Za-z0-9]{16,}"),
    # AWS access key ids, Google API keys
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    # GitHub, GitLab, Slack, Stripe, npm, Hugging Face tokens
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bhf_[A-Za-z0-9]{30,}\b"),
    # JSON Web Tokens
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    # credentials in a URL (scheme://user:password@host)
    re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]{3,}@[^\s/]+"),
)


def looks_secret(text: str) -> bool:
    """True when ``text`` matches any secret pattern."""
    if not isinstance(text, str) or not text:
        return False
    return any(p.search(text) for p in SECRET_PATTERNS)
