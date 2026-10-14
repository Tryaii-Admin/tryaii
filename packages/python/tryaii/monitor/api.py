"""HTTP to the tryaii API for monitor upload, remote config and ``tryaii apps``
(shared/monitor/API.md, SPEC.md §6).

Stdlib urllib only (httpx is not a dependency of the base install). Redirects
are never followed (the Authorization header must not travel to another
host). One attempt per call; callers own retries and backoff.

:func:`request` returns an :class:`ApiResponse` for every HTTP status and
raises :class:`ApiNetworkError` only for transport failures (connection,
timeout, unreadable body). Seams, in order of precedence:

1. ``opener=`` -- a callable with the urlopen signature (unit tests);
2. env ``TRYAII_HTTP_FIXTURE`` -- the path of a JSON file of canned
   responses (SPEC.md §6.3; the CLI fixture suites and both SDKs' parity
   drivers use it, so no test ever touches the network). With
   ``TRYAII_HTTP_FIXTURE_LOG`` set too, every request is appended to that
   file as one JSON line ``{"method", "path", "query", "auth", "body"}``
   (``auth`` = ``app_key`` | ``bearer`` | null -- never the credential);
3. the real network.
"""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.request
from typing import Optional
from urllib.parse import parse_qsl, urlparse

DEFAULT_API_URL = "https://api.tryaii.com"
TIMEOUT_SECONDS = 10.0
ENV_API_URL = "TRYAII_API_URL"
ENV_API_KEY = "TRYAII_API_KEY"
ENV_HTTP_FIXTURE = "TRYAII_HTTP_FIXTURE"
ENV_HTTP_FIXTURE_LOG = "TRYAII_HTTP_FIXTURE_LOG"

#: ``tryaii_<key_id>_<secret>`` (API.md §1).
APP_KEY_RE = re.compile(r"^tryaii_([a-z0-9]{12})_([A-Za-z0-9_-]{43})$")


class ApiNetworkError(Exception):
    """The request never produced an HTTP status (refused, timeout, ...)."""


class ApiResponse:
    """One HTTP answer: ``status``, the decoded JSON ``body`` (None when
    empty or not JSON) and the response ``headers`` (lower-cased names)."""

    __slots__ = ("status", "body", "headers")

    def __init__(self, status: int, body, headers: Optional[dict] = None):
        self.status = status
        self.body = body
        self.headers = headers or {}

    @property
    def error(self) -> Optional[str]:
        """The ``error`` code of an error body, else None."""
        if isinstance(self.body, dict) and isinstance(self.body.get("error"), str):
            return self.body["error"] or None
        return None

    @property
    def detail(self) -> Optional[str]:
        if isinstance(self.body, dict) and isinstance(self.body.get("detail"), str):
            return self.body["detail"] or None
        return None


def api_url() -> str:
    """``TRYAII_API_URL`` (an empty value falls through) or the default,
    without trailing slashes."""
    return (os.environ.get(ENV_API_URL) or DEFAULT_API_URL).rstrip("/")


def api_key() -> Optional[str]:
    """``TRYAII_API_KEY`` trimmed, or None."""
    value = (os.environ.get(ENV_API_KEY) or "").strip()
    return value or None


def is_app_key(value: Optional[str]) -> bool:
    return isinstance(value, str) and APP_KEY_RE.match(value) is not None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _version() -> str:
    from tryaii import __version__

    return __version__


def _decode(raw: bytes):
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


# --- the fixture transport (SPEC.md §6.3) --------------------------------------

_fixture_lock = threading.Lock()
_fixture_state: dict = {}


def _fixture_routes(path: str) -> dict:
    with _fixture_lock:
        routes = _fixture_state.get(path)
        if routes is None:
            try:
                doc = json.loads(open(path, encoding="utf-8").read())
            except (OSError, ValueError):
                doc = {}
            raw = doc.get("routes") if isinstance(doc, dict) else None
            routes = {k: list(v) for k, v in (raw or {}).items() if isinstance(v, list)}
            _fixture_state[path] = routes
        return routes


def _auth_kind(bearer: Optional[str]) -> Optional[str]:
    if bearer is None:
        return None
    return "app_key" if is_app_key(bearer) else "bearer"


def _fixture_request(fixture: str, method: str, url: str, body, bearer) -> ApiResponse:
    parsed = urlparse(url)
    log = os.environ.get(ENV_HTTP_FIXTURE_LOG)
    if log:
        line = {"method": method, "path": parsed.path,
                "query": dict(parse_qsl(parsed.query, keep_blank_values=True)) or None,
                "auth": _auth_kind(bearer), "body": body}
        with _fixture_lock:
            with open(log, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
    routes = _fixture_routes(fixture)
    with _fixture_lock:
        queue = routes.get(f"{method} {parsed.path}")
        if not queue:
            raise ApiNetworkError("no fixture route")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
    if not isinstance(item, dict) or item.get("network"):
        raise ApiNetworkError("fixture network failure")
    status = item.get("status")
    if not isinstance(status, int):
        raise ApiNetworkError("fixture without status")
    return ApiResponse(status, item.get("body"), {})


def reset_fixture_state() -> None:
    """Forget loaded fixture files (tests)."""
    with _fixture_lock:
        _fixture_state.clear()


# --- request -------------------------------------------------------------------

def request(method: str, url: str, body=None, *, bearer: Optional[str] = None,
            opener=None, timeout: float = TIMEOUT_SECONDS,
            raw_body: Optional[bytes] = None) -> ApiResponse:
    """Send one request; JSON ``body`` (or the exact ``raw_body`` bytes).

    Returns the answer for EVERY HTTP status; raises ApiNetworkError for a
    transport failure. Never follows redirects (a 3xx is returned as is).
    """
    if opener is None:
        fixture = os.environ.get(ENV_HTTP_FIXTURE)
        if fixture:
            logged = body
            if raw_body is not None:
                logged = _decode(raw_body)
            return _fixture_request(fixture, method, url, logged, bearer)
    headers = {"Accept": "application/json", "User-Agent": f"tryaii/{_version()}"}
    data = raw_body
    if data is None and body is not None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if data is not None:
        headers["Content-Type"] = "application/json"
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    open_fn = opener or _OPENER.open
    try:
        with open_fn(req, timeout=timeout) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            raw = response.read()
            resp_headers = _headers(response)
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001 -- an unreadable error body is just empty
            raw = b""
        resp_headers = _headers(exc)
    except Exception as exc:  # noqa: BLE001 -- one collapsed transport failure
        raise ApiNetworkError(type(exc).__name__) from exc
    return ApiResponse(int(status), _decode(raw), resp_headers)


def _headers(obj) -> dict:
    try:
        items = obj.headers.items() if getattr(obj, "headers", None) is not None else []
    except Exception:  # noqa: BLE001
        items = []
    return {str(k).lower(): str(v) for k, v in items}
