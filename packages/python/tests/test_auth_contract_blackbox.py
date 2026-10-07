"""Black-box tests for `tryaii login` / `logout` / `whoami`.

Derived ONLY from docs/auth/CONTRACT-auth-v1.md (section 4: HTTP API,
section 6: SDK / CLI, section 7: v1.1 amendments -- tests whose expectation
v1.1 changed cite the section 7 item). The CLI runs as a subprocess against a small,
self-contained fake HTTP server defined in this file; nothing here imports
the implementation or the other auth test helpers.

Every subprocess gets a temp TRYAII_DRE_DATA_DIR and a temp HOME/USERPROFILE,
so the real ~/.tryaii is never touched, and TRYAII_API_URL points at the
fake (or at a closed local port), so no real server is contacted.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PKG_DIR = Path(__file__).resolve().parents[1]

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
CRED_KEYS = {
    "version",
    "api_url",
    "user",
    "entitlements",
    "refresh_token",
    "refresh_expires_at",
    "access_token",
    "access_expires_at",
    "created_at",
}

# Distinctive values so the test proves the CLI prints what the server sent.
VERIFY_URI = "https://web.example.test/device"
USER_CODE = "BCDF-GHJK"
VERIFY_URI_COMPLETE = "https://web.example.test/device?user_code=BCDF-GHJK"
DEVICE_CODE = "dc_" + "A" * 40

LOGIN_PROMPT = (
    "To sign in, open this URL in a browser:\n"
    f"  {VERIFY_URI}\n"
    f"and enter the code: {USER_CODE}\n"
    "\n"
    f"Or open directly: {VERIFY_URI_COMPLETE}\n"
    "\n"
    "Waiting for approval (expires in 10 minutes, Ctrl+C to cancel)...\n"
)

# v1.1 (section 7, items 13 + 19): TRYAII_TOKEN lines removed from HELP_LOGIN;
# HELP_LOGOUT exit code 1 now means "could not remove the credentials file".
# Catalog contract v1 (docs/catalog/CONTRACT-catalog-v1.md section 5, "CLI
# changes"): HELP_LOGIN gains the "After signing in, ..." paragraph and the
# global HELP login line reads "(free; unlocks the full model catalog)".
HELP_LOGIN = """tryaii login -- Sign in with your tryaii.com account

Usage:
  tryaii login

Starts a device sign-in: prints a URL and a short code. Open the URL in any
browser (it does not have to be on this machine), sign in with Google and
approve the code. Works over SSH and inside containers.

Credentials are stored in ~/.tryaii/credentials.json (or under
TRYAII_DRE_DATA_DIR).

After signing in, the full model catalog is downloaded and kept up to date
automatically (checked at most once a day).

Environment:
  TRYAII_API_URL        Override the API base URL (default https://api.tryaii.com)

Examples:
  tryaii login

Exit codes:
  0 success, 1 denied, expired or network failure, 2 bad flag, 130 cancelled."""

HELP_LOGOUT = """tryaii logout -- Sign out and remove the stored credentials

Usage:
  tryaii logout

Revokes the session on the server (best effort) and deletes the local
credentials file.

Examples:
  tryaii logout

Exit codes:
  0 success or already signed out, 1 could not remove the credentials file, 2 bad flag."""

HELP_WHOAMI = """tryaii whoami -- Show the signed-in account

Usage:
  tryaii whoami [options]

Options:
  --json                Print the account as pretty-printed JSON

Examples:
  tryaii whoami
  tryaii whoami --json

Exit codes:
  0 signed in, 1 not signed in, session ended or network failure, 2 bad flag."""

# Catalog contract v1 (docs/catalog/CONTRACT-catalog-v1.md section 5): `login`
# downloads the full catalog right after "Logged in as <email>." -- this fake
# has no GET /v1/catalog/live route (404), so every login here ends with the
# failure line (stdout, exit code stays 0) -- and `whoami` prints a third
# line, "Catalog: not downloaded yet" when no full catalog is cached. The
# entitlement is catalog:full (section 4).
LOGIN_CATALOG_FAILED = "Could not download the full catalog now; it will be fetched on next use.\n"
WHOAMI_NO_CATALOG = "Catalog: not downloaded yet\n"

GLOBAL_HELP_AUTH_LINES = [
    "  login                 Sign in with your tryaii.com account (free; unlocks the full model catalog)",
    "  logout                Sign out and remove the stored credentials",
    "  whoami                Show the signed-in account",
]


# ---------------------------------------------------------------------------
# Minimal programmable fake API
# ---------------------------------------------------------------------------


class FakeApi:
    """Threaded stdlib HTTP server; responses are queued per (method, path).

    A queued response is (status, body) or (status, body, headers) where
    body is a dict/list (sent as JSON) or bytes (sent raw as text/html) and
    headers are extra response headers. The last queued response repeats.
    ``hook(method, path, fn)`` runs ``fn(record)`` before answering.
    """

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self._routes: dict[tuple[str, str], list] = {}
        self._hooks: dict[tuple[str, str], object] = {}
        self._lock = threading.Lock()
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    parsed = json.loads(raw.decode("utf-8")) if raw else None
                except ValueError:
                    parsed = None
                rec = {
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "raw": raw,
                    "json": parsed,
                    "t": time.monotonic(),
                }
                with api._lock:
                    api.requests.append(rec)
                    queue = api._routes.get((self.command, self.path))
                    if queue:
                        resp = queue.pop(0) if len(queue) > 1 else queue[0]
                    else:
                        resp = (404, {"error": "not_found"})
                    hook = api._hooks.get((self.command, self.path))
                if hook is not None:
                    hook(rec)
                status, body, *extra = resp
                extra_headers = extra[0] if extra else {}
                if isinstance(body, (bytes, bytearray)):
                    payload, ctype = bytes(body), "text/html"
                else:
                    payload, ctype = json.dumps(body).encode(), "application/json"
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                for k, v in extra_headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(payload)

            do_GET = _handle  # noqa: N815 (BaseHTTPRequestHandler API)
            do_POST = _handle  # noqa: N815 (BaseHTTPRequestHandler API)
            do_CONNECT = _handle  # noqa: N815 (BaseHTTPRequestHandler API)  # used only as an HTTPS proxy probe

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> FakeApi:
        self._thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def on(self, method: str, path: str, *responses) -> None:
        with self._lock:
            self._routes[(method, path)] = list(responses)

    def hook(self, method: str, path: str, fn) -> None:
        with self._lock:
            self._hooks[(method, path)] = fn

    def hits(self, method: str | None = None, path: str | None = None) -> list[dict]:
        with self._lock:
            return [
                r
                for r in self.requests
                if (method is None or r["method"] == method)
                and (path is None or r["path"] == path)
            ]

    def paths(self) -> list[str]:
        with self._lock:
            return [r["path"] for r in self.requests]


@pytest.fixture
def api():
    fake = FakeApi().start()
    yield fake
    fake.stop()


def closed_port_url() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def iso(dt: _dt.datetime) -> str:
    return dt.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> _dt.datetime:
    return _dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)


def now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


class Sandbox:
    def __init__(self, root: Path, api_url: str | None):
        self.root = root
        self.data_dir = root / "data"
        self.home = root / "home"
        self.data_dir.mkdir(parents=True)
        self.home.mkdir(parents=True)
        self.api_url = api_url
        self.extra_env: dict[str, str] = {}

    @property
    def creds(self) -> Path:
        return self.data_dir / "credentials.json"

    def env(self) -> dict:
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.upper().startswith("TRYAII_")
            and k.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
        }
        env["TRYAII_NO_BANNER"] = "1"
        env["TRYAII_DRE_DATA_DIR"] = str(self.data_dir)
        env["HOME"] = str(self.home)
        env["USERPROFILE"] = str(self.home)
        env["NO_PROXY"] = "127.0.0.1,localhost"
        env["no_proxy"] = "127.0.0.1,localhost"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if self.api_url is not None:
            env["TRYAII_API_URL"] = self.api_url
        env.update(self.extra_env)
        return env

    def run(self, *args: str, timeout: float = 90):
        p = subprocess.run(
            [sys.executable, "-m", "tryaii.cli.main", *args],
            cwd=str(PKG_DIR),
            env=self.env(),
            capture_output=True,
            timeout=timeout,
        )
        out = p.stdout.decode("utf-8", "replace").replace("\r\n", "\n")
        err = p.stderr.decode("utf-8", "replace").replace("\r\n", "\n")
        return p.returncode, out, err

    def seed_creds(
        self,
        *,
        access_in: float = 3600,
        refresh_token: str = "tair_seeded_refresh",
        access_token: str = "at_seeded_access",
        email: str = "old@example.com",
        name: str = "Old User",
        api_url: str | None = None,
    ) -> dict:
        t = now()
        doc = {
            "version": 1,
            "api_url": api_url or self.api_url or "https://api.tryaii.com",
            "user": {"id": "u-old", "email": email, "name": name},
            "entitlements": ["catalog:full"],
            "refresh_token": refresh_token,
            "refresh_expires_at": iso(t + _dt.timedelta(days=90)),
            "access_token": access_token,
            "access_expires_at": iso(t + _dt.timedelta(seconds=access_in)),
            "created_at": iso(t - _dt.timedelta(hours=1)),
        }
        self.creds.write_text(json.dumps(doc), encoding="utf-8")
        return doc

    def read_creds(self) -> dict:
        return json.loads(self.creds.read_text(encoding="utf-8"))

    def all_file_bytes(self) -> bytes:
        blob = b""
        for base in (self.data_dir, self.home):
            for p in base.rglob("*"):
                if p.is_file():
                    try:
                        blob += p.read_bytes()
                    except OSError:
                        pass
        return blob


@pytest.fixture
def sb(tmp_path, api):
    return Sandbox(tmp_path, api.url)


def device_resp(interval: int = 1, expires_in: int = 600) -> tuple:
    return (
        200,
        {
            "device_code": DEVICE_CODE,
            "user_code": USER_CODE,
            "verification_uri": VERIFY_URI,
            "verification_uri_complete": VERIFY_URI_COMPLETE,
            "expires_in": expires_in,
            "interval": interval,
        },
    )


def token_resp(
    *,
    access: str = "at_new_access",
    refresh: str = "tair_new_refresh",
    email: str = "a@b.com",
    name: str = "A B",
    ents=("catalog:full",),
    expires_in: int = 3600,
    refresh_expires_in: int = 7776000,
) -> tuple:
    return (
        200,
        {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": expires_in,
            "refresh_token": refresh,
            "refresh_expires_in": refresh_expires_in,
            "user": {"id": "u-1", "email": email, "name": name},
            "entitlements": list(ents),
        },
    )


def err(code: str, status: int = 400) -> tuple:
    return (status, {"error": code, "error_description": f"desc for {code}"})


def me_resp(email="a@b.com", name="A B", ents=("catalog:full",)) -> tuple:
    return (
        200,
        {
            "user": {"id": "u-1", "email": email, "name": name},
            "entitlements": list(ents),
            "session": {"id": "fam123", "created_at": "2026-10-03T12:00:00Z"},
        },
    )


def network_msg(url: str) -> str:
    return f"Could not reach {url}. Check your connection and try again.\n"


def assert_ts_near(s: str, expected: _dt.datetime, tol: float = 30.0) -> None:
    assert ISO_Z.match(s), f"not ISO-8601 Z second precision: {s!r}"
    delta = abs((parse_iso(s) - expected).total_seconds())
    assert delta <= tol, f"{s} is {delta:.0f}s away from {iso(expected)}"


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


def test_login_success_output_and_exit_code(sb, api):
    api.on("POST", "/v1/auth/device/code", device_resp())
    api.on("POST", "/v1/auth/token", token_resp())
    code, out, errout = sb.run("login")
    assert out == LOGIN_PROMPT + "Logged in as a@b.com.\n" + LOGIN_CATALOG_FAILED
    assert errout == ""
    assert code == 0


def test_login_request_bodies_and_headers(sb, api):
    api.on("POST", "/v1/auth/device/code", device_resp())
    api.on("POST", "/v1/auth/token", token_resp())
    code, _, _ = sb.run("login")
    assert code == 0

    dev = api.hits("POST", "/v1/auth/device/code")
    assert len(dev) == 1
    body = dev[0]["json"]
    assert set(body) == {"client_id", "client"}
    assert body["client_id"] == "tryaii-cli"
    client = body["client"]
    assert set(client) == {"sdk", "version", "os"}
    assert client["sdk"] == "python"
    for k in ("version", "os"):
        assert isinstance(client[k], str) and 0 < len(client[k]) <= 64

    tok = api.hits("POST", "/v1/auth/token")
    assert tok, "never polled the token endpoint"
    for r in tok:
        assert r["json"] == {
            "grant_type": DEVICE_GRANT,
            "device_code": DEVICE_CODE,
            "client_id": "tryaii-cli",
        }

    for r in api.hits():
        assert r["headers"].get("user-agent", "").startswith("tryaii/"), r["headers"]
        assert r["headers"]["user-agent"] == f"tryaii/{client['version']}"
        # Catalog contract v1 section 5: login now also sends the body-less
        # GET /v1/catalog/live; every request WITH a body is JSON.
        if r["method"] == "POST":
            assert r["headers"].get("content-type", "").split(";")[0].strip() == "application/json"


def test_login_writes_credentials_file_schema(sb, api):
    api.on("POST", "/v1/auth/device/code", device_resp())
    api.on(
        "POST",
        "/v1/auth/token",
        token_resp(access="at_X", refresh="tair_X", expires_in=3600, refresh_expires_in=7776000),
    )
    code, _, _ = sb.run("login")
    t = now()
    assert code == 0
    doc = sb.read_creds()
    assert set(doc) == CRED_KEYS
    assert doc["version"] == 1
    assert doc["api_url"] == sb.api_url
    assert doc["user"] == {"id": "u-1", "email": "a@b.com", "name": "A B"}
    assert doc["entitlements"] == ["catalog:full"]
    assert doc["refresh_token"] == "tair_X"
    assert doc["access_token"] == "at_X"
    assert_ts_near(doc["access_expires_at"], t + _dt.timedelta(seconds=3600))
    assert_ts_near(doc["refresh_expires_at"], t + _dt.timedelta(seconds=7776000))
    assert_ts_near(doc["created_at"], t)
    # Atomic write: no stray temp files left next to it.
    leftovers = [p.name for p in sb.data_dir.iterdir() if p.name != "credentials.json"]
    assert not [n for n in leftovers if "cred" in n.lower() or n.endswith((".tmp", ".temp"))]
    if os.name == "posix":
        assert (sb.creds.stat().st_mode & 0o777) == 0o600


@pytest.mark.parametrize(
    "error_code, message",
    [
        ("access_denied", "Login denied in the browser.\n"),
        ("expired_token", "The code expired. Run tryaii login again.\n"),
        ("invalid_grant", "Login failed: invalid_grant.\n"),
        ("unsupported_grant_type", "Login failed: unsupported_grant_type.\n"),
    ],
)
def test_login_poll_failures(sb, api, error_code, message):
    api.on("POST", "/v1/auth/device/code", device_resp())
    api.on("POST", "/v1/auth/token", err(error_code))
    code, out, errout = sb.run("login")
    assert out == LOGIN_PROMPT
    assert errout == message
    assert code == 1
    assert not sb.creds.exists()


def test_login_device_code_error_is_login_failed(sb, api):
    api.on("POST", "/v1/auth/device/code", err("invalid_client"))
    code, out, errout = sb.run("login")
    assert out == ""
    assert errout == "Login failed: invalid_client.\n"
    assert code == 1
    assert api.hits("POST", "/v1/auth/token") == []


def test_login_network_unreachable_port(tmp_path):
    url = closed_port_url()
    s = Sandbox(tmp_path, url)
    code, out, errout = s.run("login")
    assert out == ""
    assert errout == network_msg(url)
    assert code == 1
    assert not s.creds.exists()


def test_login_non_json_device_response_is_network(sb, api):
    api.on("POST", "/v1/auth/device/code", (200, b"<html>oops</html>"))
    code, out, errout = sb.run("login")
    assert out == ""
    assert errout == network_msg(sb.api_url)
    assert code == 1


def test_login_unexpected_status_while_polling_is_network(sb, api):
    api.on("POST", "/v1/auth/device/code", device_resp())
    api.on("POST", "/v1/auth/token", (500, b"<html>Internal Server Error</html>"))
    code, out, errout = sb.run("login")
    assert out == LOGIN_PROMPT
    assert errout == network_msg(sb.api_url)
    assert code == 1
    assert not sb.creds.exists()


def test_login_polls_at_interval_until_approved(sb, api):
    api.on("POST", "/v1/auth/device/code", device_resp(interval=1))
    api.on(
        "POST",
        "/v1/auth/token",
        err("authorization_pending"),
        err("authorization_pending"),
        token_resp(),
    )
    code, out, _ = sb.run("login")
    assert code == 0
    assert out.endswith("Logged in as a@b.com.\n" + LOGIN_CATALOG_FAILED)
    polls = api.hits("POST", "/v1/auth/token")
    assert len(polls) == 3
    gaps = [b["t"] - a["t"] for a, b in zip(polls, polls[1:])]
    for g in gaps:
        assert g >= 0.9, f"polled faster than interval=1s: gaps={gaps}"
        assert g < 4.0, f"polled far slower than interval=1s: gaps={gaps}"


def test_login_slow_down_adds_five_seconds(sb, api):
    api.on("POST", "/v1/auth/device/code", device_resp(interval=1))
    api.on(
        "POST",
        "/v1/auth/token",
        err("slow_down"),
        err("authorization_pending"),
        token_resp(),
    )
    code, out, _ = sb.run("login")
    assert code == 0, out
    polls = api.hits("POST", "/v1/auth/token")
    assert len(polls) == 3
    g1 = polls[1]["t"] - polls[0]["t"]
    g2 = polls[2]["t"] - polls[1]["t"]
    # interval 1 + 5 = 6 s, and the bump persists for later polls.
    assert g1 >= 5.8, f"gap after slow_down {g1:.2f}s < 6s"
    assert g2 >= 5.8, f"interval did not stay at 6s after slow_down: {g2:.2f}s"
    assert g1 < 9.5 and g2 < 9.5, (g1, g2)


def test_login_when_already_logged_in_replaces_and_revokes_old(sb, api):
    sb.seed_creds(refresh_token="tair_OLD", email="old@example.com")
    api.on("POST", "/v1/auth/device/code", device_resp())
    api.on("POST", "/v1/auth/token", token_resp(refresh="tair_NEW", access="at_NEW"))
    api.on("POST", "/v1/auth/revoke", (200, {}))
    code, out, errout = sb.run("login")
    assert out == (
        "Already logged in as old@example.com. Signing in again replaces that session.\n"
        + LOGIN_PROMPT
        + "Logged in as a@b.com.\n"
        + LOGIN_CATALOG_FAILED
    )
    assert errout == ""
    assert code == 0
    doc = sb.read_creds()
    assert doc["refresh_token"] == "tair_NEW"
    assert doc["access_token"] == "at_NEW"
    rev = api.hits("POST", "/v1/auth/revoke")
    assert len(rev) == 1
    assert rev[0]["json"] == {"refresh_token": "tair_OLD", "client_id": "tryaii-cli"}
    success_t = [r for r in api.hits("POST", "/v1/auth/token")][-1]["t"]
    assert rev[0]["t"] >= success_t, "old session revoked before the new login succeeded"


def test_login_already_logged_in_revoke_failure_is_ignored(sb, api):
    sb.seed_creds(refresh_token="tair_OLD", email="old@example.com")
    api.on("POST", "/v1/auth/device/code", device_resp())
    api.on("POST", "/v1/auth/token", token_resp(refresh="tair_NEW"))
    api.on("POST", "/v1/auth/revoke", (500, b"boom"))
    code, out, errout = sb.run("login")
    assert code == 0
    assert out.endswith("Logged in as a@b.com.\n" + LOGIN_CATALOG_FAILED)
    assert errout == ""
    assert sb.read_creds()["refresh_token"] == "tair_NEW"


def test_login_already_logged_in_then_denied_keeps_old_session(sb, api):
    seeded = sb.seed_creds(refresh_token="tair_OLD", email="old@example.com")
    before = sb.creds.read_bytes()
    api.on("POST", "/v1/auth/device/code", device_resp())
    api.on("POST", "/v1/auth/token", err("access_denied"))
    code, out, errout = sb.run("login")
    assert out == (
        "Already logged in as old@example.com. Signing in again replaces that session.\n"
        + LOGIN_PROMPT
    )
    assert errout == "Login denied in the browser.\n"
    assert code == 1
    # Old session is revoked only "after success".
    assert api.hits("POST", "/v1/auth/revoke") == []
    assert sb.creds.read_bytes() == before, seeded


@pytest.mark.skipif(os.name != "posix", reason="Ctrl+C delivery to a child needs POSIX signals")
def test_login_ctrl_c_cancels_with_130(sb, api):
    api.on("POST", "/v1/auth/device/code", device_resp(interval=1))
    api.on("POST", "/v1/auth/token", err("authorization_pending"))
    p = subprocess.Popen(
        [sys.executable, "-m", "tryaii.cli.main", "login"],
        cwd=str(PKG_DIR),
        env=sb.env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 30
    while not api.hits("POST", "/v1/auth/token") and time.monotonic() < deadline:
        time.sleep(0.1)
    p.send_signal(signal.SIGINT)
    out, errout = p.communicate(timeout=30)
    assert errout.decode().replace("\r\n", "\n") == "Login cancelled.\n"
    assert p.returncode == 130


# ---------------------------------------------------------------------------
# logout
# ---------------------------------------------------------------------------


def test_logout_with_file_revokes_and_deletes(sb, api):
    sb.seed_creds(refresh_token="tair_LOGOUT")
    api.on("POST", "/v1/auth/revoke", (200, {}))
    code, out, errout = sb.run("logout")
    assert out == "Logged out.\n"
    assert errout == ""
    assert code == 0
    assert not sb.creds.exists()
    assert api.paths() == ["/v1/auth/revoke"]
    r = api.hits("POST", "/v1/auth/revoke")[0]
    assert r["json"] == {"refresh_token": "tair_LOGOUT", "client_id": "tryaii-cli"}
    assert r["headers"].get("user-agent", "").startswith("tryaii/")


def test_logout_revoke_error_is_ignored(sb, api):
    sb.seed_creds()
    api.on("POST", "/v1/auth/revoke", (500, b"<html>down</html>"))
    code, out, errout = sb.run("logout")
    assert (code, out, errout) == (0, "Logged out.\n", "")
    assert not sb.creds.exists()


def test_logout_unreachable_server_is_ignored(tmp_path):
    s = Sandbox(tmp_path, closed_port_url())
    s.seed_creds()
    code, out, errout = s.run("logout")
    assert (code, out, errout) == (0, "Logged out.\n", "")
    assert not s.creds.exists()


def test_logout_not_logged_in(sb, api):
    code, out, errout = sb.run("logout")
    assert (code, out, errout) == (0, "Not logged in.\n", "")
    assert api.requests == []


@pytest.mark.parametrize("with_file", [True, False])
def test_logout_ignores_env_token(sb, api, with_file):
    # v1.1 (section 7, item 13): TRYAII_TOKEN is not supported and is
    # ignored, so it no longer blocks logout; the file session (if any) is
    # revoked and removed as usual, and the env value is never sent.
    if with_file:
        sb.seed_creds(refresh_token="tair_FILE")
    api.on("POST", "/v1/auth/revoke", (200, {}))
    sb.extra_env["TRYAII_TOKEN"] = "tair_from_env"
    code, out, errout = sb.run("logout")
    assert errout == ""
    assert code == 0
    assert not sb.creds.exists()
    if with_file:
        assert out == "Logged out.\n"
        assert [r["json"] for r in api.hits("POST", "/v1/auth/revoke")] == [
            {"refresh_token": "tair_FILE", "client_id": "tryaii-cli"}]
    else:
        assert out == "Not logged in.\n"
        assert api.requests == []
    assert all(b"tair_from_env" not in r["raw"] for r in api.requests)


# ---------------------------------------------------------------------------
# whoami
# ---------------------------------------------------------------------------


def test_whoami_success_without_refresh(sb, api):
    sb.seed_creds(access_in=3600, access_token="at_VALID")
    api.on("GET", "/v1/auth/me", me_resp(ents=("catalog:full", "beta:x")))
    code, out, errout = sb.run("whoami")
    assert out == "Logged in as a@b.com (A B)\nEntitlements: catalog:full, beta:x\n" + WHOAMI_NO_CATALOG
    assert errout == ""
    assert code == 0
    assert api.paths() == ["/v1/auth/me"]
    me = api.hits("GET", "/v1/auth/me")[0]
    assert me["headers"].get("authorization") == "Bearer at_VALID"
    assert me["headers"].get("user-agent", "").startswith("tryaii/")


def test_whoami_no_entitlements_prints_none(sb, api):
    sb.seed_creds()
    api.on("GET", "/v1/auth/me", me_resp(ents=()))
    code, out, _ = sb.run("whoami")
    assert code == 0
    assert out == "Logged in as a@b.com (A B)\nEntitlements: none\n" + WHOAMI_NO_CATALOG


def test_whoami_json_keeps_server_key_order(sb, api):
    sb.seed_creds()
    body = {
        "session": {"created_at": "2026-10-03T12:00:00Z", "id": "fam123"},
        "user": {"name": "A B", "id": "u-1", "email": "a@b.com"},
        "entitlements": ["catalog:full"],
    }
    api.on("GET", "/v1/auth/me", (200, body))
    code, out, errout = sb.run("whoami", "--json")
    assert errout == ""
    assert code == 0
    assert out == json.dumps(body, indent=2) + "\n"


def test_whoami_not_logged_in(sb, api):
    code, out, errout = sb.run("whoami")
    assert (code, out, errout) == (1, "", "Not logged in. Run: tryaii login\n")
    assert api.requests == []


@pytest.mark.parametrize("access_in", [-120, -1, 0, 30, 59])
def test_whoami_refreshes_when_expired_or_within_60s(sb, api, access_in):
    sb.seed_creds(access_in=access_in, refresh_token="tair_R1", access_token="at_OLD")
    api.on(
        "POST",
        "/v1/auth/token",
        token_resp(access="at_R2", refresh="tair_R2", expires_in=3600, refresh_expires_in=7776000),
    )
    api.on("GET", "/v1/auth/me", me_resp())
    code, out, errout = sb.run("whoami")
    t = now()
    assert errout == ""
    assert code == 0
    assert out == "Logged in as a@b.com (A B)\nEntitlements: catalog:full\n" + WHOAMI_NO_CATALOG
    assert api.paths() == ["/v1/auth/token", "/v1/auth/me"]
    assert api.hits("POST", "/v1/auth/token")[0]["json"] == {
        "grant_type": "refresh_token",
        "refresh_token": "tair_R1",
        "client_id": "tryaii-cli",
    }
    assert api.hits("GET", "/v1/auth/me")[0]["headers"].get("authorization") == "Bearer at_R2"
    # Rotated refresh token and new access token are persisted.
    doc = sb.read_creds()
    assert set(doc) == CRED_KEYS
    assert doc["refresh_token"] == "tair_R2"
    assert doc["access_token"] == "at_R2"
    assert_ts_near(doc["access_expires_at"], t + _dt.timedelta(seconds=3600))
    assert_ts_near(doc["refresh_expires_at"], t + _dt.timedelta(seconds=7776000))
    for k in ("refresh_expires_at", "access_expires_at", "created_at"):
        assert ISO_Z.match(doc[k]), (k, doc[k])


@pytest.mark.parametrize("access_in", [90, 300, 3600])
def test_whoami_does_not_refresh_with_more_than_60s_left(sb, api, access_in):
    sb.seed_creds(access_in=access_in, access_token="at_KEEP", refresh_token="tair_KEEP")
    api.on("GET", "/v1/auth/me", me_resp())
    code, _, _ = sb.run("whoami")
    assert code == 0
    assert api.hits("POST", "/v1/auth/token") == []
    assert api.hits("GET", "/v1/auth/me")[0]["headers"].get("authorization") == "Bearer at_KEEP"
    assert sb.read_creds()["refresh_token"] == "tair_KEEP"


def test_whoami_refresh_rejected_ends_session(sb, api):
    sb.seed_creds(access_in=-10)
    api.on("POST", "/v1/auth/token", err("invalid_grant"))
    code, out, errout = sb.run("whoami")
    assert (code, out, errout) == (1, "", "Your session has ended. Run: tryaii login\n")
    assert not sb.creds.exists()
    assert api.hits("GET", "/v1/auth/me") == []


def test_whoami_me_401_ends_session(sb, api):
    sb.seed_creds(access_in=3600)
    api.on("GET", "/v1/auth/me", (401, {"error": "invalid_token"}))
    code, out, errout = sb.run("whoami")
    assert (code, out, errout) == (1, "", "Your session has ended. Run: tryaii login\n")
    assert not sb.creds.exists()


def test_whoami_network_unreachable_keeps_file(tmp_path):
    url = closed_port_url()
    s = Sandbox(tmp_path, url)
    s.seed_creds(access_in=3600)
    before = s.creds.read_bytes()
    code, out, errout = s.run("whoami")
    assert (code, out, errout) == (1, "", network_msg(url))
    assert s.creds.read_bytes() == before


def test_whoami_network_during_refresh_keeps_file(tmp_path):
    url = closed_port_url()
    s = Sandbox(tmp_path, url)
    s.seed_creds(access_in=-10)
    before = s.creds.read_bytes()
    code, out, errout = s.run("whoami")
    assert (code, out, errout) == (1, "", network_msg(url))
    assert s.creds.read_bytes() == before


def test_whoami_me_server_error_is_network(sb, api):
    sb.seed_creds(access_in=3600)
    api.on("GET", "/v1/auth/me", (500, b"<html>err</html>"))
    code, out, errout = sb.run("whoami")
    assert (code, out, errout) == (1, "", network_msg(sb.api_url))
    assert sb.creds.exists()


# ---------------------------------------------------------------------------
# TRYAII_TOKEN
# ---------------------------------------------------------------------------


def test_env_token_alone_is_ignored_and_never_written(sb, api):
    # v1.1 (section 7, item 13): TRYAII_TOKEN is ignored -- with no
    # credentials file the CLI is simply not logged in, sends nothing and
    # still writes nothing to disk.
    sb.extra_env["TRYAII_TOKEN"] = "tair_ENV_SECRET"
    api.on(
        "POST",
        "/v1/auth/token",
        token_resp(access="at_ENV_ACCESS", refresh="tair_ENV_ROTATED"),
    )
    api.on("GET", "/v1/auth/me", me_resp())
    code, out, errout = sb.run("whoami")
    assert (code, out, errout) == (1, "", "Not logged in. Run: tryaii login\n")
    assert api.requests == []
    assert not sb.creds.exists()
    blob = sb.all_file_bytes()
    for secret in (b"tair_ENV_SECRET", b"at_ENV_ACCESS", b"tair_ENV_ROTATED"):
        assert secret not in blob, f"{secret!r} was written to disk"


def test_env_token_does_not_override_the_file(sb, api):
    # v1.1 (section 7, item 13): the file session is used; the env value is
    # never sent anywhere.
    sb.seed_creds(access_in=3600, access_token="at_FILE", refresh_token="tair_FILE")
    before = sb.creds.read_bytes()
    sb.extra_env["TRYAII_TOKEN"] = "tair_ENV"
    api.on("POST", "/v1/auth/token", token_resp(access="at_ENV2", refresh="tair_ENV2"))
    api.on("GET", "/v1/auth/me", me_resp())
    code, _, _ = sb.run("whoami")
    assert code == 0
    assert api.hits("POST", "/v1/auth/token") == []
    assert api.hits("GET", "/v1/auth/me")[0]["headers"].get("authorization") == "Bearer at_FILE"
    assert sb.creds.read_bytes() == before
    assert all(b"tair_ENV" not in r["raw"] for r in api.requests)


def test_env_token_does_not_replace_the_file_on_refresh(sb, api):
    # v1.1 (section 7, item 13): an expired file session refreshes with the
    # FILE's refresh token (rotation persisted); TRYAII_TOKEN plays no part,
    # so a server rejecting it cannot end the file session.
    sb.seed_creds(access_in=-10, refresh_token="tair_FILE")
    sb.extra_env["TRYAII_TOKEN"] = "tair_ENV_BAD"
    api.on("POST", "/v1/auth/token", token_resp(access="at_R2", refresh="tair_R2"))
    api.on("GET", "/v1/auth/me", me_resp())
    code, out, errout = sb.run("whoami")
    assert (code, errout) == (0, "")
    assert [r["json"]["refresh_token"] for r in api.hits("POST", "/v1/auth/token")] == ["tair_FILE"]
    assert sb.read_creds()["refresh_token"] == "tair_R2"


# ---------------------------------------------------------------------------
# TRYAII_API_URL default
# ---------------------------------------------------------------------------


def test_empty_api_url_falls_back_to_default(tmp_path, api):
    """Route the CLI's traffic into the fake via a proxy so nothing real is hit.

    A control run first proves the CLI honours proxy env vars (using a
    `.invalid` host that can never resolve); if it does not, the test is
    skipped rather than risk contacting the real api.tryaii.com.
    """
    api.on("CONNECT", "api.tryaii.com:443", (403, b"forbidden"))

    ctrl = Sandbox(tmp_path / "ctrl", "http://tryaii-blackbox.invalid")
    ctrl.extra_env.update({"HTTP_PROXY": api.url, "http_proxy": api.url, "NO_PROXY": "", "no_proxy": ""})
    ctrl.run("login", timeout=60)
    if not any("tryaii-blackbox.invalid" in p for p in api.paths()):
        pytest.skip("CLI does not route through HTTP(S)_PROXY; unsafe to test the real default")

    s = Sandbox(tmp_path / "real", "")
    s.extra_env.update({"HTTPS_PROXY": api.url, "https_proxy": api.url, "NO_PROXY": "", "no_proxy": ""})
    code, out, errout = s.run("login", timeout=60)
    assert any(r["method"] == "CONNECT" and r["path"] == "api.tryaii.com:443" for r in api.requests)
    assert out == ""
    assert errout == network_msg("https://api.tryaii.com")
    assert code == 1


# ---------------------------------------------------------------------------
# flags and help
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [("login", "--bogus"), ("logout", "--bogus"), ("whoami", "--bogus"), ("login", "--json")],
)
def test_bad_flags_exit_2(sb, api, argv):
    code, out, _ = sb.run(*argv)
    assert code == 2
    assert api.requests == []
    assert not sb.creds.exists()


@pytest.mark.parametrize(
    "cmd, text", [("login", HELP_LOGIN), ("logout", HELP_LOGOUT), ("whoami", HELP_WHOAMI)]
)
def test_help_command_text(sb, api, cmd, text):
    code, out, errout = sb.run("help", cmd)
    assert code == 0
    assert out == text + "\n"
    assert api.requests == []


def test_login_dash_h_prints_help(sb, api):
    code, out, _ = sb.run("login", "-h")
    assert code == 0
    assert out == HELP_LOGIN + "\n"
    assert api.requests == []


def test_global_help_lists_auth_commands_after_regenerate(sb):
    code, out, _ = sb.run("help")
    assert code == 0
    lines = out.split("\n")
    idx = next(i for i, ln in enumerate(lines) if ln.startswith("  regenerate "))
    assert lines[idx + 1 : idx + 4] == GLOBAL_HELP_AUTH_LINES


# ---------------------------------------------------------------------------
# v1.1 amendments (section 7, items 13-21)
# ---------------------------------------------------------------------------

RATE_LIMITED_MSG = "Too many sign-in attempts. Wait a minute and try again.\n"
CANNOT_DELETE_MSG = "Could not remove the credentials file.\n"
SESSION_ENDED_MSG = "Your session has ended. Run: tryaii login\n"


def unexpected_msg(url: str, code: str) -> str:
    return f"Unexpected response from {url}: {code}.\n"


@pytest.fixture
def other_api():
    fake = FakeApi().start()
    yield fake
    fake.stop()


# -- item 13: TRYAII_API_URL is for login only -------------------------------


@pytest.mark.parametrize("access_in", [3600, -10])
def test_v11_whoami_talks_to_the_stored_api_url_not_the_env(tmp_path, api, other_api, access_in):
    s = Sandbox(tmp_path, other_api.url)  # TRYAII_API_URL -> other host
    s.seed_creds(api_url=api.url, access_in=access_in, refresh_token="tair_STORED")
    for fake in (api, other_api):
        fake.on("POST", "/v1/auth/token", token_resp(access="at_R2", refresh="tair_R2"))
        fake.on("GET", "/v1/auth/me", me_resp())
    code, out, errout = s.run("whoami")
    assert (code, errout) == (0, "")
    assert out == "Logged in as a@b.com (A B)\nEntitlements: catalog:full\n" + WHOAMI_NO_CATALOG
    assert other_api.requests == [], "refresh token or access token sent to TRYAII_API_URL"
    assert api.paths() == (["/v1/auth/token"] if access_in < 0 else []) + ["/v1/auth/me"]
    assert s.read_creds()["api_url"] == api.url


def test_v11_whoami_rejection_at_env_host_cannot_delete_the_file(tmp_path, api, other_api):
    # The review case: the env host would reject the production token.
    s = Sandbox(tmp_path, other_api.url)
    s.seed_creds(api_url=api.url, access_in=-10)
    other_api.on("POST", "/v1/auth/token", err("invalid_grant"))
    api.on("POST", "/v1/auth/token", token_resp())
    api.on("GET", "/v1/auth/me", me_resp())
    code, _, errout = s.run("whoami")
    assert (code, errout) == (0, "")
    assert other_api.requests == []
    assert s.creds.exists()


def test_v11_whoami_network_message_names_the_stored_url(tmp_path, api):
    dead = closed_port_url()
    s = Sandbox(tmp_path, api.url)
    s.seed_creds(api_url=dead)
    code, out, errout = s.run("whoami")
    assert (code, out, errout) == (1, "", network_msg(dead))
    assert api.requests == []


def test_v11_logout_revokes_at_the_stored_api_url(tmp_path, api, other_api):
    s = Sandbox(tmp_path, other_api.url)
    s.seed_creds(api_url=api.url, refresh_token="tair_STORED")
    api.on("POST", "/v1/auth/revoke", (200, {}))
    code, out, errout = s.run("logout")
    assert (code, out, errout) == (0, "Logged out.\n", "")
    assert other_api.requests == []
    assert [r["json"]["refresh_token"] for r in api.hits("POST", "/v1/auth/revoke")] == ["tair_STORED"]


def test_v11_login_uses_env_url_and_revokes_old_session_at_its_own_url(tmp_path, api, other_api):
    s = Sandbox(tmp_path, other_api.url)
    s.seed_creds(api_url=api.url, refresh_token="tair_OLD")
    other_api.on("POST", "/v1/auth/device/code", device_resp())
    other_api.on("POST", "/v1/auth/token", token_resp(refresh="tair_NEW"))
    other_api.on("POST", "/v1/auth/revoke", (200, {}))
    api.on("POST", "/v1/auth/revoke", (200, {}))
    code, _, errout = s.run("login")
    assert (code, errout) == (0, "")
    assert s.read_creds()["api_url"] == other_api.url
    assert other_api.hits("POST", "/v1/auth/revoke") == []
    assert [r["json"]["refresh_token"] for r in api.hits("POST", "/v1/auth/revoke")] == ["tair_OLD"]


# -- item 14: only invalid_grant / 401 end the session -----------------------


@pytest.mark.parametrize(
    "route, reply, access_in, code",
    [
        (("POST", "/v1/auth/token"), err("invalid_request"), -10, "invalid_request"),
        (("POST", "/v1/auth/token"), err("invalid_client", 401), -10, "invalid_client"),
        (("POST", "/v1/auth/token"), err("unsupported_grant_type"), -10, "unsupported_grant_type"),
        (("GET", "/v1/auth/me"), err("invalid_request"), 3600, "invalid_request"),
    ],
)
def test_v11_whoami_other_oauth_errors_keep_the_file(sb, api, route, reply, access_in, code):
    sb.seed_creds(access_in=access_in)
    before = sb.creds.read_bytes()
    api.on(*route, reply)
    code_, out, errout = sb.run("whoami")
    assert (code_, out, errout) == (1, "", unexpected_msg(sb.api_url, code))
    assert sb.creds.read_bytes() == before


def test_v11_whoami_me_401_with_any_code_ends_the_session(sb, api):
    sb.seed_creds()
    api.on("GET", "/v1/auth/me", err("invalid_client", 401))
    assert sb.run("whoami") == (1, "", SESSION_ENDED_MSG)
    assert not sb.creds.exists()


# -- item 15: 429 = rate_limited ---------------------------------------------


@pytest.mark.parametrize("reply", [(429, {"error": "slow_down"}), (429, b"Too Many Requests")])
def test_v11_login_429_on_device_code(sb, api, reply):
    api.on("POST", "/v1/auth/device/code", reply)
    assert sb.run("login") == (1, "", RATE_LIMITED_MSG)
    assert not sb.creds.exists()


def test_v11_login_429_while_polling_stops(sb, api):
    api.on("POST", "/v1/auth/device/code", device_resp())
    api.on("POST", "/v1/auth/token", (429, {"error": "slow_down"}))
    assert sb.run("login") == (1, LOGIN_PROMPT, RATE_LIMITED_MSG)
    assert len(api.hits("POST", "/v1/auth/token")) == 1


def test_v11_login_503_stays_network(sb, api):
    api.on("POST", "/v1/auth/device/code", (503, {"error": "temporarily_unavailable"}))
    assert sb.run("login") == (1, "", network_msg(sb.api_url))


@pytest.mark.parametrize("access_in, route", [(-10, ("POST", "/v1/auth/token")),
                                              (3600, ("GET", "/v1/auth/me"))])
def test_v11_whoami_429_keeps_the_file(sb, api, access_in, route):
    sb.seed_creds(access_in=access_in)
    before = sb.creds.read_bytes()
    api.on(*route, (429, {"error": "slow_down"}))
    assert sb.run("whoami") == (1, "", unexpected_msg(sb.api_url, "rate_limited"))
    assert sb.creds.read_bytes() == before


# -- item 16: redirects are never followed -----------------------------------


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_v11_whoami_redirect_is_network_and_not_followed(sb, api, status):
    sb.seed_creds(access_token="at_SECRET")
    api.on("GET", "/v1/auth/me", (status, b"", {"Location": f"{api.url}/elsewhere"}))
    api.on("GET", "/elsewhere", me_resp())
    assert sb.run("whoami") == (1, "", network_msg(sb.api_url))
    assert api.paths() == ["/v1/auth/me"]
    assert sb.creds.exists()


@pytest.mark.parametrize("status", [302, 307])
def test_v11_login_redirect_is_network_and_not_followed(sb, api, status):
    api.on("POST", "/v1/auth/device/code", (status, b"", {"Location": f"{api.url}/elsewhere"}))
    api.on("POST", "/elsewhere", device_resp())
    api.on("GET", "/elsewhere", device_resp())
    assert sb.run("login") == (1, "", network_msg(sb.api_url))
    assert api.paths() == ["/v1/auth/device/code"]


# -- item 17: polling defaults and the deadline ------------------------------


def test_v11_interval_zero_defaults_to_5_seconds(sb, api):
    api.on("POST", "/v1/auth/device/code", device_resp(interval=0))
    api.on("POST", "/v1/auth/token", token_resp())
    code, _, _ = sb.run("login")
    assert code == 0
    dev = api.hits("POST", "/v1/auth/device/code")[0]["t"]
    first = api.hits("POST", "/v1/auth/token")[0]["t"]
    assert first - dev >= 4.8, f"first poll after {first - dev:.2f}s, expected the 5 s default"


@pytest.mark.parametrize("expires_in", [0, -5])
def test_v11_expires_in_not_positive_defaults_to_600(sb, api, expires_in):
    api.on("POST", "/v1/auth/device/code", device_resp(interval=1, expires_in=expires_in))
    api.on("POST", "/v1/auth/token", err("authorization_pending"), err("authorization_pending"),
           token_resp())
    code, out, errout = sb.run("login")
    assert (code, errout) == (0, "")
    assert out == LOGIN_PROMPT + "Logged in as a@b.com.\n" + LOGIN_CATALOG_FAILED


def test_v11_approval_in_the_last_interval_wins(sb, api):
    # expires_in 1 < interval 2: the first poll already happens after the
    # local deadline -- it is the one final poll and its approval counts.
    api.on("POST", "/v1/auth/device/code", device_resp(interval=2, expires_in=1))
    api.on("POST", "/v1/auth/token", token_resp())
    code, out, errout = sb.run("login")
    assert (code, errout) == (0, "")
    assert out == LOGIN_PROMPT + "Logged in as a@b.com.\n" + LOGIN_CATALOG_FAILED


@pytest.mark.parametrize("late", ["authorization_pending", "slow_down", "invalid_grant"])
def test_v11_late_answers_after_the_deadline_are_expired(sb, api, late):
    api.on("POST", "/v1/auth/device/code", device_resp(interval=2, expires_in=1))
    api.on("POST", "/v1/auth/token", err(late))
    code, out, errout = sb.run("login")
    assert (code, out, errout) == (1, LOGIN_PROMPT, "The code expired. Run tryaii login again.\n")
    assert len(api.hits("POST", "/v1/auth/token")) == 1, "exactly one poll after the deadline"


# -- item 18: credentials write ----------------------------------------------


def test_v11_refresh_write_leaves_only_the_credentials_file(sb, api):
    sb.seed_creds(access_in=-10)
    api.on("POST", "/v1/auth/token", token_resp(refresh="tair_R2"))
    api.on("GET", "/v1/auth/me", me_resp())
    assert sb.run("whoami")[0] == 0
    assert sorted(p.name for p in sb.data_dir.iterdir()) == ["credentials.json"]
    assert sb.read_creds()["refresh_token"] == "tair_R2"
    if os.name == "posix":
        assert (sb.creds.stat().st_mode & 0o777) == 0o600


@pytest.mark.skipif(os.name != "nt", reason="rename-over-open-file only fails on Windows")
def test_v11_windows_rename_survives_a_briefly_open_credentials_file(sb, api):
    # Another process holding credentials.json open (antivirus, indexer)
    # makes the rename fail with EACCES/EPERM for a moment; the rotated
    # refresh token must still be saved.
    sb.seed_creds(access_in=-10, refresh_token="tair_R1")
    held = []

    def hold_file(_rec):
        fh = open(sb.creds, "rb")
        held.append(fh)
        threading.Timer(0.12, fh.close).start()

    api.hook("POST", "/v1/auth/token", hold_file)
    api.on("POST", "/v1/auth/token", token_resp(refresh="tair_R2"))
    api.on("GET", "/v1/auth/me", me_resp())
    code, out, errout = sb.run("whoami")
    assert (code, errout) == (0, ""), errout
    assert sb.read_creds()["refresh_token"] == "tair_R2"
    assert held


# -- item 19: logout cannot delete -------------------------------------------


def test_v11_logout_cannot_delete_reports_and_exits_1(sb, api):
    sb.creds.mkdir()
    (sb.creds / "keep").write_text("x", encoding="utf-8")
    code, out, errout = sb.run("logout")
    assert (code, out, errout) == (1, "", CANNOT_DELETE_MSG)
    assert sb.creds.is_dir()


# -- item 20: UTF-8 stdout ----------------------------------------------------


def test_v11_non_cp1252_email_prints_as_utf8(sb, api):
    email, name = "dév.測試@例子.example", "Zoë 測試"
    sb.extra_env.update({"PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"})
    api.on("POST", "/v1/auth/device/code", device_resp())
    api.on("POST", "/v1/auth/token", token_resp(email=email, name=name))
    api.on("GET", "/v1/auth/me", me_resp(email=email, name=name))
    code, out, errout = sb.run("login")
    assert (code, errout) == (0, "")
    assert out == LOGIN_PROMPT + f"Logged in as {email}.\n" + LOGIN_CATALOG_FAILED
    code, out, errout = sb.run("whoami")
    assert (code, out, errout) == (
        0, f"Logged in as {email} ({name})\nEntitlements: catalog:full\n" + WHOAMI_NO_CATALOG, "")
    code, out, _ = sb.run("whoami", "--json")
    assert code == 0 and json.loads(out)["user"]["name"] == name
    api.on("POST", "/v1/auth/revoke", (200, {}))
    assert sb.run("logout") == (0, "Logged out.\n", "")


# -- item 21: help topics -----------------------------------------------------


def test_v11_help_topics_include_auth_commands(sb):
    code, out, _ = sb.run("help", "help")
    assert code == 0
    topics = out[out.index("Topics:") + len("Topics:"):out.index("Examples:")]
    names = [t.strip() for t in topics.replace("\n", " ").split(",")]
    for cmd in ("login", "logout", "whoami"):
        assert cmd in names, names
