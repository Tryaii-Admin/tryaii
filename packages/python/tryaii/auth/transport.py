"""HTTP transport for the auth API (CONTRACT-auth-v1.md section 6, "Transport").

Stdlib urllib only -- httpx is not a dependency of the base install and must
not become one. One attempt per request, 10 s timeout, no retries (polling is
the loop). Outcomes:

- 200 with a JSON object body          -> that dict
- 400/401 with a JSON ``{"error": ...}`` -> ``AuthError(<error code>)``
- 429 (any body)                         -> ``AuthError("rate_limited")`` (v1.1 item 15)
- anything else (connection failure, timeout, non-JSON body, unexpected
  status, any 3xx -- redirects are never followed, v1.1 item 16)
                                         -> ``AuthError("network")``

so the CLI messages stay byte-identical across platforms and SDKs.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Optional

DEFAULT_API_URL = "https://api.tryaii.com"
TIMEOUT_SECONDS = 10.0
CLIENT_ID = "tryaii-cli"

# The one collapsed failure kind.
NETWORK = "network"
# HTTP 429 from any endpoint (CONTRACT section 7, item 15).
RATE_LIMITED = "rate_limited"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect (CONTRACT section 7, item 16): urllib would
    otherwise re-send the request -- Authorization header included -- to
    whatever host the Location names. Returning None makes the 3xx surface
    as an HTTPError, which ``request_json`` maps to ``network``."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _open_no_redirect(request, timeout=None):
    return _OPENER.open(request, timeout=timeout)


# Module-level seam: tests monkeypatch this to a fake with the urlopen
# signature when they cannot pass ``opener=`` down (e.g. through the CLI).
urlopen = _open_no_redirect

_OAUTH_ERROR_STATUSES = (400, 401)


class AuthError(Exception):
    """A failed auth API call. ``code`` is the OAuth ``error`` value, or
    ``"network"`` for every transport-level failure."""

    def __init__(self, code: str, *, status: Optional[int] = None,
                 description: Optional[str] = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.description = description

    @property
    def is_network(self) -> bool:
        return self.code == NETWORK

    @property
    def is_rate_limited(self) -> bool:
        return self.code == RATE_LIMITED


def effective_api_url() -> str:
    """The api base for ``login``: ``TRYAII_API_URL`` first (an empty value
    falls through), else the default. Trailing slashes are dropped so paths
    join cleanly. A stored session never uses this (see
    :func:`session_api_url`)."""
    return (os.environ.get("TRYAII_API_URL") or DEFAULT_API_URL).rstrip("/")


def session_api_url(stored) -> str:
    """The api base for an existing session: ALWAYS the ``api_url`` recorded
    in its credentials file, never ``TRYAII_API_URL`` (CONTRACT section 7,
    item 13 -- the refresh token must not be sent to another host). A file
    without one falls back to the default, not the environment."""
    if isinstance(stored, str) and stored:
        return stored.rstrip("/")
    return DEFAULT_API_URL


def _version() -> str:
    from tryaii import __version__

    return __version__


def _parse_object(raw: bytes) -> Optional[dict]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def request_json(method: str, url: str, body: Optional[dict] = None, *,
                 bearer: Optional[str] = None, opener=None,
                 timeout: float = TIMEOUT_SECONDS) -> dict:
    """Send one JSON request and return the decoded 200 body.

    ``opener`` is the injectable seam (house style, as in
    designpartner/transport.py): a callable with the urlopen signature.
    Raises ``AuthError`` only.
    """
    headers = {
        "Accept": "application/json",
        "User-Agent": f"tryaii/{_version()}",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    open_fn = opener or urlopen

    try:
        with open_fn(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001 -- unreadable error body = network
            raw = b""
    except Exception as exc:  # noqa: BLE001 -- contract: one collapsed kind
        raise AuthError(NETWORK) from exc

    if status == 200:
        parsed = _parse_object(raw)
        if parsed is None:
            raise AuthError(NETWORK, status=status)
        return parsed
    if status == 429:
        raise AuthError(RATE_LIMITED, status=status)
    if status in _OAUTH_ERROR_STATUSES:
        parsed = _parse_object(raw)
        if parsed is not None and isinstance(parsed.get("error"), str) and parsed["error"]:
            description = parsed.get("error_description")
            raise AuthError(parsed["error"], status=status,
                            description=description if isinstance(description, str) else None)
    raise AuthError(NETWORK, status=status)
