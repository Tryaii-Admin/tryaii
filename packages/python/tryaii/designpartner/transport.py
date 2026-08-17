"""Submission transport (SPEC.md §5).

Stdlib urllib only — httpx is not a dependency of the base install and
must not become one. One attempt, 10s timeout, and EVERY failure (network
error, timeout, non-2xx) collapses into a single False outcome so the CLI
message stays byte-identical across platforms and SDKs.
"""

from __future__ import annotations

import os
import urllib.request

DEFAULT_URL = "https://designpartners.tryaii.com/api"
TIMEOUT_SECONDS = 10.0


def effective_url() -> str:
    """Env override first (the TRYAII_DESIGNPARTNER_URL convention), else
    the default endpoint. An empty env value falls through."""
    return os.environ.get("TRYAII_DESIGNPARTNER_URL") or DEFAULT_URL


def send_submission(url: str, body: bytes, *, version: str,
                    opener=None, timeout: float = TIMEOUT_SECONDS) -> bool:
    """POST the exact saved submission bytes. True only on a 2xx response.

    `opener` is the injectable seam (house style): a callable with the
    urlopen signature. Never raises.
    """
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": f"tryaii/{version}",
        },
    )
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            return 200 <= status < 300
    except Exception:  # noqa: BLE001 -- SPEC §5: one collapsed failure branch
        return False
