"""Device flow, refresh, revoke and /me (CONTRACT-auth-v1.md sections 1, 4, 6).

Every function takes the injectable seams it needs: ``opener`` (urlopen
signature), ``sleep`` and ``clock`` (epoch seconds). Defaults resolve at call
time through module attributes so tests can monkeypatch them.
"""

from __future__ import annotations

import math
import sys
import time
from typing import Callable, Optional

from tryaii.auth import store
from tryaii.auth.transport import CLIENT_ID, AuthError, request_json

DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
SLOW_DOWN_STEP = 5
REFRESH_SKEW_SECONDS = 60

# Call-time defaults (monkeypatchable seams).
sleep: Callable[[float], None] = time.sleep
clock: Callable[[], float] = time.time


def client_info() -> dict:
    from tryaii import __version__

    return {"sdk": "python", "version": __version__, "os": sys.platform}


def start_device(api_url: str, *, opener=None) -> dict:
    """``POST /v1/auth/device/code``."""
    return request_json("POST", f"{api_url}/v1/auth/device/code",
                        {"client_id": CLIENT_ID, "client": client_info()},
                        opener=opener)


def poll_for_token(api_url: str, device: dict, *, opener=None,
                   sleep_fn: Optional[Callable[[float], None]] = None,
                   clock_fn: Optional[Callable[[], float]] = None) -> dict:
    """Poll ``POST /v1/auth/token`` until approved (CONTRACT section 7, item 17).

    ``interval`` / ``expires_in`` that are missing, non-numeric or ``<= 0``
    default to 5 / 600. Waits ``interval`` seconds before every poll;
    ``authorization_pending`` keeps polling, ``slow_down`` adds 5 s to the
    interval. The deadline is checked after each answer, so the client polls
    exactly once more after the local deadline (an approval in the last
    interval still wins); a pending/slow_down answer -- or an
    ``invalid_grant`` -- received after the deadline raises
    ``AuthError("expired_token")``. Any other error is raised as is.
    """
    sleep_fn = sleep_fn or sleep
    clock_fn = clock_fn or clock
    interval = _positive_or(device.get("interval"), 5)
    deadline = clock_fn() + _positive_or(device.get("expires_in"), 600)
    body = {"grant_type": DEVICE_GRANT_TYPE,
            "device_code": device.get("device_code"),
            "client_id": CLIENT_ID}
    while True:
        sleep_fn(interval)
        try:
            return request_json("POST", f"{api_url}/v1/auth/token", body,
                                opener=opener)
        except AuthError as exc:
            expired = clock_fn() >= deadline
            if exc.code in ("authorization_pending", "slow_down", "invalid_grant") and expired:
                raise AuthError("expired_token", status=exc.status) from exc
            if exc.code == "authorization_pending":
                continue
            if exc.code == "slow_down":
                interval += SLOW_DOWN_STEP
                continue
            raise


def refresh(api_url: str, refresh_token: str, *, opener=None) -> dict:
    """``POST /v1/auth/token`` with the refresh grant (rotates the token)."""
    return request_json("POST", f"{api_url}/v1/auth/token",
                        {"grant_type": "refresh_token",
                         "refresh_token": refresh_token,
                         "client_id": CLIENT_ID},
                        opener=opener)


def revoke(api_url: str, refresh_token: str, *, opener=None) -> bool:
    """``POST /v1/auth/revoke``, best effort: never raises; True on 200."""
    try:
        request_json("POST", f"{api_url}/v1/auth/revoke",
                     {"refresh_token": refresh_token, "client_id": CLIENT_ID},
                     opener=opener)
    except Exception:  # noqa: BLE001 -- best effort by contract
        return False
    return True


def me(api_url: str, access_token: str, *, opener=None) -> dict:
    """``GET /v1/auth/me``. A 401 surfaces as ``AuthError("invalid_token")``."""
    return request_json("GET", f"{api_url}/v1/auth/me",
                        bearer=access_token, opener=opener)


def credentials_from_token(api_url: str, token: dict, now: float, *,
                           created_at: Optional[str] = None) -> dict:
    """Build the stored-credentials document from a token response."""
    return {
        "version": store.SCHEMA_VERSION,
        "api_url": api_url,
        "user": token.get("user"),
        "entitlements": token.get("entitlements", []),
        "refresh_token": token.get("refresh_token"),
        "refresh_expires_at": store.iso_z(
            now + _positive_number(token.get("refresh_expires_in"), 0)),
        "access_token": token.get("access_token"),
        "access_expires_at": store.iso_z(
            now + _positive_number(token.get("expires_in"), 0)),
        "created_at": created_at or store.iso_z(now),
    }


def needs_refresh(creds: dict, now: float) -> bool:
    """True when there is no access token or it expires within 60 s."""
    if not creds.get("access_token"):
        return True
    expires = store.parse_iso_z(creds.get("access_expires_at"))
    return expires is None or now >= expires - REFRESH_SKEW_SECONDS


def refresh_if_needed(api_url: str, creds: dict, *, opener=None,
                      clock_fn: Optional[Callable[[], float]] = None
                      ) -> tuple[dict, bool]:
    """Return ``(creds, refreshed)``. Refreshes (rotating the refresh token)
    when the access token is missing or within 60 s of expiry; the caller
    decides whether to persist. Raises ``AuthError`` on rejection/network."""
    now = (clock_fn or clock)()
    if not needs_refresh(creds, now):
        return creds, False
    token = refresh(api_url, creds["refresh_token"], opener=opener)
    return credentials_from_token(api_url, token, now,
                                  created_at=creds.get("created_at")), True


def _positive_or(value, default: float) -> float:
    """``value`` when it is a finite number > 0, else ``default``."""
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        return default
    return value


def _positive_number(value, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return default
    return value
