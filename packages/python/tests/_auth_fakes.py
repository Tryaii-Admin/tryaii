"""Shared fakes for the auth tests: a scripted urlopen stand-in."""

from __future__ import annotations

import io
import json
import urllib.error
from urllib.parse import urlparse

TOKEN_OK = {
    "access_token": "eyJ.access.1", "token_type": "Bearer", "expires_in": 3600,
    "refresh_token": "tair_new", "refresh_expires_in": 7776000,
    "user": {"id": "u1", "email": "a@b.com", "name": "A B"},
    "entitlements": ["catalog:full"],
}

DEVICE_OK = {
    "device_code": "dc-1", "user_code": "BCDF-GHJK",
    "verification_uri": "https://tryaii.com/device",
    "verification_uri_complete": "https://tryaii.com/device?user_code=BCDF-GHJK",
    "expires_in": 600, "interval": 5,
}

ME_OK = {
    "user": {"id": "u1", "email": "a@b.com", "name": "A B"},
    "entitlements": ["catalog:full"],
    "session": {"id": "fam1", "created_at": "2026-10-03T12:00:00Z"},
}

NETWORK = "network"  # sentinel: raise a connection error


class FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _encode(payload) -> bytes:
    if isinstance(payload, bytes):
        return payload
    return json.dumps(payload).encode("utf-8")


class FakeOpener:
    """``routes`` maps a URL path to a list of responses served in order (the
    last one repeats). A response is ``(status, dict|bytes)`` or ``NETWORK``.
    Every call is recorded in ``calls`` as dicts."""

    def __init__(self, routes: dict):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls = []

    def paths(self):
        return [c["path"] for c in self.calls]

    def __call__(self, request, timeout=None):
        path = urlparse(request.full_url).path
        body = json.loads(request.data.decode("utf-8")) if request.data else None
        self.calls.append({
            "method": request.get_method(), "url": request.full_url, "path": path,
            "body": body, "headers": dict(request.header_items()), "timeout": timeout,
        })
        queue = self.routes.get(path)
        if not queue:
            raise urllib.error.URLError("no route")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if item == NETWORK:
            raise urllib.error.URLError("connection refused")
        status, payload = item
        data = _encode(payload)
        if 200 <= status < 300:
            return FakeResponse(status, data)
        raise urllib.error.HTTPError(request.full_url, status, "error", {},
                                     io.BytesIO(data))
