"""Unit tests for tryaii.auth (docs/auth/CONTRACT-auth-v1.md section 6):
transport error collapsing, credentials storage, device-flow polling,
refresh timing, revoke and /me."""

from __future__ import annotations

import errno
import json
import os
import socket
import sys

import pytest

from tests._auth_fakes import DEVICE_OK, ME_OK, NETWORK, TOKEN_OK, FakeOpener
from tryaii import __version__
from tryaii.auth import flow, store, transport
from tryaii.auth.transport import AuthError

API = "https://api.example.test"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Never touch the real ~/.tryaii or a real API."""
    monkeypatch.setenv("TRYAII_DRE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("TRYAII_TOKEN", raising=False)
    monkeypatch.delenv("TRYAII_API_URL", raising=False)

    def _no_network(*a, **k):
        raise AssertionError("real network used in a unit test")

    monkeypatch.setattr(transport, "urlopen", _no_network)


# --- transport --------------------------------------------------------------


def test_api_url_default_env_and_empty(monkeypatch):
    assert transport.effective_api_url() == "https://api.tryaii.com"
    monkeypatch.setenv("TRYAII_API_URL", "")
    assert transport.effective_api_url() == "https://api.tryaii.com"
    monkeypatch.setenv("TRYAII_API_URL", "http://127.0.0.1:9/")
    assert transport.effective_api_url() == "http://127.0.0.1:9"


def test_request_sends_json_user_agent_timeout_and_bearer():
    opener = FakeOpener({"/v1/auth/me": [(200, ME_OK)]})
    assert transport.request_json("GET", f"{API}/v1/auth/me", bearer="tok",
                                  opener=opener) == ME_OK
    call = opener.calls[0]
    assert call["headers"]["User-agent"] == f"tryaii/{__version__}"
    assert call["headers"]["Authorization"] == "Bearer tok"
    assert call["timeout"] == 10.0
    assert call["method"] == "GET"

    opener = FakeOpener({"/x": [(200, {})]})
    transport.request_json("POST", f"{API}/x", {"a": 1}, opener=opener)
    assert opener.calls[0]["headers"]["Content-type"] == "application/json"
    assert opener.calls[0]["body"] == {"a": 1}


@pytest.mark.parametrize("status,code", [(400, "authorization_pending"),
                                         (400, "invalid_grant"),
                                         (401, "invalid_token")])
def test_oauth_errors_surface_their_code(status, code):
    opener = FakeOpener({"/x": [(status, {"error": code, "error_description": "d"})]})
    with pytest.raises(AuthError) as info:
        transport.request_json("POST", f"{API}/x", {}, opener=opener)
    assert info.value.code == code
    assert info.value.status == status
    assert not info.value.is_network


@pytest.mark.parametrize("response", [
    NETWORK,                               # connection failure
    (200, b"<html>not json</html>"),       # non-JSON 200
    (200, b"[1, 2]"),                      # JSON but not an object
    (500, {"error": "server_error"}),      # unexpected status
    (503, {"error": "temporarily_unavailable"}),  # v1.1 item 15: 503 stays network
    (404, b"nope"),
    (302, b""),                            # v1.1 item 16: 3xx = network
    (307, {"error": "invalid_grant"}),
    (400, b"not json"),                    # 400 without an OAuth body
    (400, {"no_error": True}),
])
def test_everything_else_collapses_to_network(response):
    opener = FakeOpener({"/x": [response]})
    with pytest.raises(AuthError) as info:
        transport.request_json("POST", f"{API}/x", {}, opener=opener)
    assert info.value.code == "network"


def test_timeout_collapses_to_network():
    def opener(request, timeout=None):
        raise socket.timeout("timed out")

    with pytest.raises(AuthError) as info:
        transport.request_json("GET", f"{API}/x", opener=opener)
    assert info.value.is_network


# --- store ------------------------------------------------------------------


CREDS = {
    "version": 1, "api_url": API,
    "user": {"id": "u1", "email": "a@b.com", "name": "A B"},
    "entitlements": ["catalog:full"],
    "refresh_token": "tair_x", "refresh_expires_at": "2027-01-01T00:00:00Z",
    "access_token": "eyJ", "access_expires_at": "2026-10-03T13:00:00Z",
    "created_at": "2026-10-03T12:00:00Z",
}


def test_store_path_follows_data_dir(tmp_path, monkeypatch):
    assert store.credentials_path() == tmp_path / "data" / "credentials.json"
    monkeypatch.setenv("TRYAII_DRE_DATA_DIR", "")
    assert store.credentials_path() == store.Path.home() / ".tryaii" / "credentials.json"


def test_store_roundtrip_schema_order_and_delete():
    path = store.save(dict(reversed(list(CREDS.items()))))
    assert store.load() == CREDS
    assert list(json.loads(path.read_text(encoding="utf-8"))) == list(CREDS)
    assert path.read_bytes().endswith(b"}\n")
    assert store.delete() is True
    assert store.delete() is False
    assert store.load() is None


@pytest.mark.parametrize("content", ["not json", "[]", '{"version": 2, "refresh_token": "t"}',
                                     '{"version": 1}'])
def test_store_load_rejects_bad_files(content):
    path = store.credentials_path()
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    assert store.load() is None


def test_store_write_is_atomic(monkeypatch):
    store.save(CREDS)
    path = store.credentials_path()
    before = path.read_bytes()
    seen = {}

    def boom(src, dst):
        seen["src"] = src
        raise OSError("disk full")

    monkeypatch.setattr(store.os, "replace", boom)
    with pytest.raises(OSError):
        store.save({**CREDS, "refresh_token": "tair_other"})
    # The original file is untouched and the temp file was cleaned up.
    assert path.read_bytes() == before
    assert os.path.dirname(seen["src"]) == str(path.parent)
    assert sorted(p.name for p in path.parent.iterdir()) == ["credentials.json"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_store_file_mode_0600():
    path = store.save(CREDS)
    assert (path.stat().st_mode & 0o777) == 0o600
    store.save({**CREDS, "refresh_token": "tair_2"})
    assert (path.stat().st_mode & 0o777) == 0o600


def test_iso_z_roundtrip():
    assert store.iso_z(0) == "1970-01-01T00:00:00Z"
    assert store.iso_z(1791028800.9) == "2026-10-03T12:00:00Z"
    assert store.parse_iso_z("2026-10-03T12:00:00Z") == 1791028800.0
    assert store.parse_iso_z("garbage") is None
    assert store.parse_iso_z(None) is None


def test_tryaii_token_support_is_gone():
    # v1.1 item 13: TRYAII_TOKEN is not supported (rotation made CI tokens
    # single-use); nothing reads it any more.
    assert not hasattr(store, "env_token")


# --- flow -------------------------------------------------------------------


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


def test_client_info():
    assert flow.client_info() == {"sdk": "python", "version": __version__,
                                  "os": sys.platform}


def test_start_device_request_body():
    opener = FakeOpener({"/v1/auth/device/code": [(200, DEVICE_OK)]})
    assert flow.start_device(API, opener=opener) == DEVICE_OK
    assert opener.calls[0]["body"] == {"client_id": "tryaii-cli",
                                       "client": flow.client_info()}


def test_poll_pending_then_approved():
    clock = Clock()
    opener = FakeOpener({"/v1/auth/token": [
        (400, {"error": "authorization_pending"}),
        (400, {"error": "authorization_pending"}),
        (200, TOKEN_OK)]})
    token = flow.poll_for_token(API, DEVICE_OK, opener=opener,
                                sleep_fn=clock.sleep, clock_fn=clock)
    assert token == TOKEN_OK
    assert clock.sleeps == [5, 5, 5]
    assert opener.calls[0]["body"] == {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": "dc-1", "client_id": "tryaii-cli"}


def test_poll_slow_down_adds_five_seconds_each_time():
    clock = Clock()
    opener = FakeOpener({"/v1/auth/token": [
        (400, {"error": "slow_down"}),
        (400, {"error": "authorization_pending"}),
        (400, {"error": "slow_down"}),
        (200, TOKEN_OK)]})
    flow.poll_for_token(API, DEVICE_OK, opener=opener,
                        sleep_fn=clock.sleep, clock_fn=clock)
    assert clock.sleeps == [5, 10, 10, 15]


@pytest.mark.parametrize("code", ["access_denied", "expired_token", "invalid_grant"])
def test_poll_terminal_errors_raise(code):
    clock = Clock()
    opener = FakeOpener({"/v1/auth/token": [(400, {"error": code})]})
    with pytest.raises(AuthError) as info:
        flow.poll_for_token(API, DEVICE_OK, opener=opener,
                            sleep_fn=clock.sleep, clock_fn=clock)
    assert info.value.code == code


def test_poll_network_failure_raises_network():
    clock = Clock()
    opener = FakeOpener({"/v1/auth/token": [NETWORK]})
    with pytest.raises(AuthError) as info:
        flow.poll_for_token(API, DEVICE_OK, opener=opener,
                            sleep_fn=clock.sleep, clock_fn=clock)
    assert info.value.is_network


def test_poll_stops_locally_when_code_expires():
    clock = Clock()
    opener = FakeOpener({"/v1/auth/token": [(400, {"error": "authorization_pending"})]})
    with pytest.raises(AuthError) as info:
        flow.poll_for_token(API, {**DEVICE_OK, "expires_in": 12}, opener=opener,
                            sleep_fn=clock.sleep, clock_fn=clock)
    assert info.value.code == "expired_token"
    # v1.1 item 17: polled at t+5 and t+10, then ONE final poll after the
    # deadline (t+15) before giving up.
    assert len(opener.calls) == 3


def test_credentials_from_token_schema():
    creds = flow.credentials_from_token(API, TOKEN_OK, 1791028800.0)
    assert creds == {
        "version": 1, "api_url": API,
        "user": TOKEN_OK["user"], "entitlements": ["catalog:full"],
        "refresh_token": "tair_new", "refresh_expires_at": "2027-01-01T12:00:00Z",
        "access_token": "eyJ.access.1", "access_expires_at": "2026-10-03T13:00:00Z",
        "created_at": "2026-10-03T12:00:00Z",
    }
    kept = flow.credentials_from_token(API, TOKEN_OK, 1791028800.0,
                                       created_at="2026-01-01T00:00:00Z")
    assert kept["created_at"] == "2026-01-01T00:00:00Z"


@pytest.mark.parametrize("offset,expected", [(3600, False), (61, False),
                                             (60, True), (0, True), (-10, True)])
def test_needs_refresh_window(offset, expected):
    now = 1791028800.0
    creds = {**CREDS, "access_expires_at": store.iso_z(now + offset)}
    assert flow.needs_refresh(creds, now) is expected


def test_needs_refresh_without_access_token():
    assert flow.needs_refresh({"refresh_token": "tair_env"}, 0) is True


def test_refresh_if_needed_rotates_and_keeps_created_at():
    now = store.parse_iso_z(CREDS["access_expires_at"])  # already expired
    opener = FakeOpener({"/v1/auth/token": [(200, TOKEN_OK)]})
    creds, refreshed = flow.refresh_if_needed(API, CREDS, opener=opener,
                                              clock_fn=lambda: now)
    assert refreshed
    assert creds["refresh_token"] == "tair_new"
    assert creds["created_at"] == CREDS["created_at"]
    assert opener.calls[0]["body"] == {"grant_type": "refresh_token",
                                       "refresh_token": "tair_x",
                                       "client_id": "tryaii-cli"}


def test_refresh_if_needed_skips_fresh_token():
    now = store.parse_iso_z(CREDS["access_expires_at"]) - 3000
    opener = FakeOpener({})
    assert flow.refresh_if_needed(API, CREDS, opener=opener,
                                  clock_fn=lambda: now) == (CREDS, False)
    assert opener.calls == []


def test_revoke_is_best_effort():
    opener = FakeOpener({"/v1/auth/revoke": [(200, {})]})
    assert flow.revoke(API, "tair_x", opener=opener) is True
    assert opener.calls[0]["body"] == {"refresh_token": "tair_x",
                                       "client_id": "tryaii-cli"}
    assert flow.revoke(API, "tair_x", opener=FakeOpener({"/v1/auth/revoke": [NETWORK]})) is False


def test_me_401_is_invalid_token():
    opener = FakeOpener({"/v1/auth/me": [(401, {"error": "invalid_token"})]})
    with pytest.raises(AuthError) as info:
        flow.me(API, "eyJ", opener=opener)
    assert info.value.code == "invalid_token"
    assert info.value.status == 401


# --- v1.1 amendments (CONTRACT section 7, items 13-18) -----------------------


def test_session_api_url_is_the_stored_one_never_the_env(monkeypatch):
    # Item 13: TRYAII_API_URL applies to login only.
    monkeypatch.setenv("TRYAII_API_URL", "http://elsewhere.test")
    assert transport.effective_api_url() == "http://elsewhere.test"
    assert transport.session_api_url("https://issuer.test//") == "https://issuer.test"
    assert transport.session_api_url(None) == "https://api.tryaii.com"
    assert transport.session_api_url("") == "https://api.tryaii.com"


@pytest.mark.parametrize("payload", [{"error": "slow_down"}, b"Too Many Requests", {}])
@pytest.mark.parametrize("path", ["/v1/auth/device/code", "/v1/auth/token",
                                  "/v1/auth/revoke", "/v1/auth/me"])
def test_429_from_any_endpoint_is_rate_limited(path, payload):
    # Item 15.
    opener = FakeOpener({path: [(429, payload)]})
    with pytest.raises(AuthError) as info:
        transport.request_json("POST", f"{API}{path}", {}, opener=opener)
    assert info.value.code == "rate_limited"
    assert info.value.is_rate_limited and not info.value.is_network
    assert info.value.status == 429


@pytest.fixture
def redirect_server():
    """A real loopback server: /start redirects (status chosen per request
    via /start?<status>) to /target, which would answer a valid 200."""
    import http.server
    import threading

    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _go(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            hits.append((self.command, self.path, self.headers.get("Authorization")))
            if self.path.startswith("/start"):
                self.send_response(int(self.path.split("?")[1]))
                self.send_header("Location", "/target")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = json.dumps(ME_OK).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _go  # noqa: N815 (BaseHTTPRequestHandler API)
        do_POST = _go  # noqa: N815 (BaseHTTPRequestHandler API)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", hits
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_redirects_are_never_followed(redirect_server, status, method):
    # Item 16: the default opener (the one the CLI uses) must not follow a
    # 3xx -- urllib's would re-send the Authorization header to the target.
    url, hits = redirect_server
    body = {"a": 1} if method == "POST" else None
    with pytest.raises(AuthError) as info:
        transport.request_json(method, f"{url}/start?{status}", body, bearer="secret",
                               opener=transport._open_no_redirect)
    assert info.value.is_network
    assert info.value.status == status
    assert [h[1] for h in hits] == [f"/start?{status}"]


def test_cli_seam_defaults_to_the_no_redirect_opener(monkeypatch):
    # The module seam the CLI goes through is the no-redirect opener, not
    # urllib.request.urlopen (undo the autouse fixture's replacement first).
    monkeypatch.undo()
    assert transport.urlopen is transport._open_no_redirect


@pytest.mark.parametrize("bad", [0, -3, None, "5", True, float("nan")])
def test_poll_interval_defaults_to_5_when_not_positive(bad):
    # Item 17: interval <= 0 (or not a number) -> 5.
    clock = Clock()
    opener = FakeOpener({"/v1/auth/token": [
        (400, {"error": "authorization_pending"}), (200, TOKEN_OK)]})
    flow.poll_for_token(API, {**DEVICE_OK, "interval": bad}, opener=opener,
                        sleep_fn=clock.sleep, clock_fn=clock)
    assert clock.sleeps == [5, 5]


@pytest.mark.parametrize("bad", [0, -1, None, "600"])
def test_poll_expires_in_defaults_to_600_when_not_positive(bad):
    # Item 17: expires_in <= 0 -> 600 (polls at t+100 ... t+600, the one at
    # the deadline is the final one).
    clock = Clock()
    opener = FakeOpener({"/v1/auth/token": [(400, {"error": "authorization_pending"})]})
    with pytest.raises(AuthError) as info:
        flow.poll_for_token(API, {**DEVICE_OK, "interval": 100, "expires_in": bad},
                            opener=opener, sleep_fn=clock.sleep, clock_fn=clock)
    assert info.value.code == "expired_token"
    assert len(opener.calls) == 6


def test_poll_approval_in_the_final_interval_still_wins():
    # Item 17: approved after the last in-time poll -> the one poll after the
    # local deadline picks it up instead of reporting the code expired.
    clock = Clock()
    opener = FakeOpener({"/v1/auth/token": [
        (400, {"error": "authorization_pending"}),
        (400, {"error": "authorization_pending"}),
        (200, TOKEN_OK)]})
    token = flow.poll_for_token(API, {**DEVICE_OK, "expires_in": 12}, opener=opener,
                                sleep_fn=clock.sleep, clock_fn=clock)
    assert token == TOKEN_OK
    assert clock.sleeps == [5, 5, 5]


@pytest.mark.parametrize("late", ["invalid_grant", "slow_down", "authorization_pending"])
def test_poll_answers_after_the_deadline_are_expired(late):
    # Item 17: an invalid_grant (the server may have purged the grant) or a
    # still-pending answer received after the local deadline = expired_token.
    clock = Clock()
    opener = FakeOpener({"/v1/auth/token": [
        (400, {"error": "authorization_pending"}),
        (400, {"error": "authorization_pending"}),
        (400, {"error": late})]})
    with pytest.raises(AuthError) as info:
        flow.poll_for_token(API, {**DEVICE_OK, "expires_in": 12}, opener=opener,
                            sleep_fn=clock.sleep, clock_fn=clock)
    assert info.value.code == "expired_token"
    assert len(opener.calls) == 3


def test_poll_deadline_is_judged_when_the_answer_arrives():
    # A slow response that comes back after the deadline counts as late.
    clock = Clock()
    inner = FakeOpener({"/v1/auth/token": [(400, {"error": "invalid_grant"})]})

    def slow(request, timeout=None):
        clock.t += 20  # the request itself takes 20 s
        return inner(request, timeout)

    with pytest.raises(AuthError) as info:
        flow.poll_for_token(API, {**DEVICE_OK, "expires_in": 12}, opener=slow,
                            sleep_fn=clock.sleep, clock_fn=clock)
    assert info.value.code == "expired_token"


class _FlakyReplace:
    """os.replace stand-in failing the first ``fails`` calls with ``err``."""

    def __init__(self, fails, err):
        self.fails = fails
        self.err = err
        self.calls = 0
        self.real = os.replace

    def __call__(self, src, dst):
        self.calls += 1
        if self.calls <= self.fails:
            raise OSError(self.err, "file in use")
        self.real(src, dst)


@pytest.fixture
def windows_rename(monkeypatch):
    sleeps = []
    monkeypatch.setattr(store, "_IS_WINDOWS", True)
    monkeypatch.setattr(store, "_sleep", sleeps.append)
    return sleeps


@pytest.mark.parametrize("err", [errno.EACCES, errno.EPERM, errno.EBUSY])
def test_windows_rename_is_retried_on_transient_locks(monkeypatch, windows_rename, err):
    # Item 18: a rotated refresh token is not lost to a transient lock.
    store.save(CREDS)
    flaky = _FlakyReplace(2, err)
    monkeypatch.setattr(store.os, "replace", flaky)
    store.save({**CREDS, "refresh_token": "tair_rotated"})
    assert flaky.calls == 3
    assert windows_rename == [0.05, 0.05]
    assert store.load()["refresh_token"] == "tair_rotated"


def test_windows_rename_gives_up_after_five_retries(monkeypatch, windows_rename):
    store.save(CREDS)
    before = store.credentials_path().read_bytes()
    flaky = _FlakyReplace(99, errno.EACCES)
    monkeypatch.setattr(store.os, "replace", flaky)
    with pytest.raises(PermissionError):
        store.save({**CREDS, "refresh_token": "tair_rotated"})
    assert flaky.calls == 6  # 1 attempt + 5 retries
    assert windows_rename == [0.05] * 5
    path = store.credentials_path()
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == ["credentials.json"]


def test_rename_other_errors_and_other_platforms_do_not_retry(monkeypatch):
    sleeps = []
    monkeypatch.setattr(store, "_sleep", sleeps.append)
    store.save(CREDS)
    # Not Windows: no retry even on EACCES.
    monkeypatch.setattr(store, "_IS_WINDOWS", False)
    flaky = _FlakyReplace(1, errno.EACCES)
    monkeypatch.setattr(store.os, "replace", flaky)
    with pytest.raises(PermissionError):
        store.save(CREDS)
    assert flaky.calls == 1
    # Windows, but a non-transient error: no retry.
    monkeypatch.setattr(store, "_IS_WINDOWS", True)
    flaky = _FlakyReplace(1, errno.ENOSPC)
    monkeypatch.setattr(store.os, "replace", flaky)
    with pytest.raises(OSError):
        store.save(CREDS)
    assert flaky.calls == 1
    assert sleeps == []


def test_save_creates_the_temp_file_exclusively_0600_and_fsyncs_before_rename(monkeypatch):
    # Item 18: O_EXCL + mode 0600 at creation, fsync, then rename.
    events = []
    real_open, real_fsync, real_replace = os.open, os.fsync, os.replace

    def spy_open(path, flags, mode=0o777, *args, **kwargs):
        if os.path.basename(str(path)).startswith(".credentials-"):
            events.append(("open", bool(flags & os.O_EXCL), bool(flags & os.O_CREAT), mode))
        return real_open(path, flags, mode, *args, **kwargs)

    def spy_fsync(fd):
        events.append(("fsync",))
        return real_fsync(fd)

    def spy_replace(src, dst):
        events.append(("replace",))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(os, "replace", spy_replace)
    store.save(CREDS)
    assert events == [("open", True, True, 0o600), ("fsync",), ("replace",)]


def test_delete_missing_file_is_not_an_error():
    # Item 19: ENOENT during delete is fine.
    assert store.delete() is False


def test_delete_failure_raises():
    # Item 19: any other failure surfaces (logout reports it).
    path = store.credentials_path()
    path.mkdir(parents=True)
    (path / "keep").write_text("x", encoding="utf-8")
    with pytest.raises(OSError):
        store.delete()
    assert path.exists()
