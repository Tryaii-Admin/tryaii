"""CLI behaviour of `tryaii login` / `logout` / `whoami`
(docs/auth/CONTRACT-auth-v1.md section 6). Output strings and exit codes are
pinned byte-for-byte: the Node CLI prints the same ones."""

from __future__ import annotations

import json
import sys

import pytest

from tests._auth_fakes import DEVICE_OK, ME_OK, NETWORK, TOKEN_OK, FakeOpener
from tryaii.auth import flow, store, transport
from tryaii.cli import main as cli_main

API = "https://api.example.test"
NOW = 1791028800.0  # 2026-10-03T12:00:00Z

LOGIN_PROMPT = (
    "To sign in, open this URL in a browser:\n"
    "  https://tryaii.com/device\n"
    "and enter the code: BCDF-GHJK\n"
    "\n"
    "Or open directly: https://tryaii.com/device?user_code=BCDF-GHJK\n"
    "\n"
    "Waiting for approval (expires in 10 minutes, Ctrl+C to cancel)...\n"
)
UNREACHABLE = f"Could not reach {API}. Check your connection and try again.\n"
ENDED = "Your session has ended. Run: tryaii login\n"
# Catalog contract (docs/catalog/CONTRACT-catalog-v1.md section 5): login
# downloads the full catalog right after "Logged in as" (the scripted opener
# has no /v1/catalog/live route -> the failure line), whoami adds a third line.
LOGIN_DL_FAILED = "Could not download the full catalog now; it will be fetched on next use.\n"
NO_CATALOG = "Catalog: not downloaded yet\n"


def _creds(**overrides):
    base = {
        "version": 1, "api_url": API,
        "user": {"id": "u0", "email": "old@b.com", "name": "Old"},
        "entitlements": ["catalog:full"],
        "refresh_token": "tair_old", "refresh_expires_at": "2027-01-01T00:00:00Z",
        "access_token": "eyJ.old", "access_expires_at": "2026-10-03T13:00:00Z",
        "created_at": "2026-10-01T00:00:00Z",
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("TRYAII_NO_BANNER", "1")
    monkeypatch.setenv("TRYAII_DRE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRYAII_API_URL", API)
    monkeypatch.delenv("TRYAII_TOKEN", raising=False)
    monkeypatch.setattr(flow, "sleep", lambda s: None)
    monkeypatch.setattr(flow, "clock", lambda: NOW)


def _serve(monkeypatch, routes):
    opener = FakeOpener(routes)
    monkeypatch.setattr(transport, "urlopen", opener)
    return opener


def _run(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["tryaii", *argv])
    code = 0
    try:
        cli_main.cli()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    out = capsys.readouterr()
    return code, out.out, out.err


LOGIN_OK_ROUTES = {
    "/v1/auth/device/code": [(200, DEVICE_OK)],
    "/v1/auth/token": [(400, {"error": "authorization_pending"}), (200, TOKEN_OK)],
    "/v1/auth/revoke": [(200, {})],
}


# --- login ------------------------------------------------------------------


def test_login_pending_then_approved(monkeypatch, capsys):
    opener = _serve(monkeypatch, LOGIN_OK_ROUTES)
    code, out, err = _run(monkeypatch, capsys, "login")
    assert (code, err) == (0, "")
    assert out == LOGIN_PROMPT + "Logged in as a@b.com.\n" + LOGIN_DL_FAILED
    assert store.load() == {
        "version": 1, "api_url": API,
        "user": TOKEN_OK["user"], "entitlements": ["catalog:full"],
        "refresh_token": "tair_new", "refresh_expires_at": "2027-01-01T12:00:00Z",
        "access_token": "eyJ.access.1", "access_expires_at": "2026-10-03T13:00:00Z",
        "created_at": "2026-10-03T12:00:00Z",
    }
    assert opener.paths() == ["/v1/auth/device/code", "/v1/auth/token", "/v1/auth/token",
                              "/v1/catalog/live"]
    assert opener.calls[0]["body"]["client"]["sdk"] == "python"


def test_login_slow_down_raises_interval(monkeypatch, capsys):
    sleeps = []
    monkeypatch.setattr(flow, "sleep", sleeps.append)
    _serve(monkeypatch, {
        "/v1/auth/device/code": [(200, DEVICE_OK)],
        "/v1/auth/token": [(400, {"error": "slow_down"}),
                           (400, {"error": "authorization_pending"}),
                           (200, TOKEN_OK)],
    })
    code, out, _ = _run(monkeypatch, capsys, "login")
    assert code == 0
    assert sleeps == [5, 10, 10]


@pytest.mark.parametrize("error,message", [
    ("access_denied", "Login denied in the browser.\n"),
    ("expired_token", "The code expired. Run tryaii login again.\n"),
    ("invalid_grant", "Login failed: invalid_grant.\n"),
])
def test_login_failures(monkeypatch, capsys, error, message):
    _serve(monkeypatch, {"/v1/auth/device/code": [(200, DEVICE_OK)],
                         "/v1/auth/token": [(400, {"error": error})]})
    code, out, err = _run(monkeypatch, capsys, "login")
    assert (code, out, err) == (1, LOGIN_PROMPT, message)
    assert not store.credentials_path().exists()


def test_login_device_code_oauth_error(monkeypatch, capsys):
    _serve(monkeypatch, {"/v1/auth/device/code": [(400, {"error": "invalid_client"})]})
    assert _run(monkeypatch, capsys, "login") == (1, "", "Login failed: invalid_client.\n")


def test_login_network_failure_on_start(monkeypatch, capsys):
    _serve(monkeypatch, {"/v1/auth/device/code": [NETWORK]})
    assert _run(monkeypatch, capsys, "login") == (1, "", UNREACHABLE)


def test_login_network_failure_while_polling(monkeypatch, capsys):
    _serve(monkeypatch, {"/v1/auth/device/code": [(200, DEVICE_OK)],
                         "/v1/auth/token": [NETWORK]})
    assert _run(monkeypatch, capsys, "login") == (1, LOGIN_PROMPT, UNREACHABLE)


def test_login_default_api_url_in_network_message(monkeypatch, capsys):
    monkeypatch.setenv("TRYAII_API_URL", "")
    opener = _serve(monkeypatch, {"/v1/auth/device/code": [NETWORK]})
    code, _, err = _run(monkeypatch, capsys, "login")
    assert code == 1
    assert err == ("Could not reach https://api.tryaii.com. "
                   "Check your connection and try again.\n")
    assert opener.calls[0]["url"] == "https://api.tryaii.com/v1/auth/device/code"


def test_login_ctrl_c_cancels(monkeypatch, capsys):
    _serve(monkeypatch, {"/v1/auth/device/code": [(200, DEVICE_OK)],
                         "/v1/auth/token": [(400, {"error": "authorization_pending"})]})

    def interrupt(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(flow, "sleep", interrupt)
    assert _run(monkeypatch, capsys, "login") == (130, LOGIN_PROMPT, "Login cancelled.\n")
    assert not store.credentials_path().exists()


def test_login_replaces_existing_session_and_revokes_old(monkeypatch, capsys):
    store.save(_creds())
    opener = _serve(monkeypatch, LOGIN_OK_ROUTES)
    code, out, err = _run(monkeypatch, capsys, "login")
    assert (code, err) == (0, "")
    assert out == ("Already logged in as old@b.com. Signing in again replaces "
                   "that session.\n" + LOGIN_PROMPT + "Logged in as a@b.com.\n"
                   + LOGIN_DL_FAILED)
    assert store.load()["refresh_token"] == "tair_new"
    revokes = [c for c in opener.calls if c["path"] == "/v1/auth/revoke"]
    assert [c["body"] for c in revokes] == [{"refresh_token": "tair_old",
                                             "client_id": "tryaii-cli"}]


def test_login_replacement_succeeds_even_if_revoke_fails(monkeypatch, capsys):
    store.save(_creds())
    _serve(monkeypatch, {**LOGIN_OK_ROUTES, "/v1/auth/revoke": [NETWORK]})
    code, out, err = _run(monkeypatch, capsys, "login")
    assert (code, err) == (0, "")
    assert out.endswith("Logged in as a@b.com.\n" + LOGIN_DL_FAILED)


def test_login_failure_keeps_existing_session(monkeypatch, capsys):
    store.save(_creds())
    opener = _serve(monkeypatch, {"/v1/auth/device/code": [(200, DEVICE_OK)],
                                  "/v1/auth/token": [(400, {"error": "access_denied"})]})
    code, _, _ = _run(monkeypatch, capsys, "login")
    assert code == 1
    assert store.load() == _creds()
    assert "/v1/auth/revoke" not in opener.paths()


RATE_LIMITED = "Too many sign-in attempts. Wait a minute and try again.\n"


@pytest.mark.parametrize("payload", [{"error": "slow_down"}, b"Too Many Requests"])
def test_login_429_on_device_code(monkeypatch, capsys, payload):
    # v1.1 item 15.
    _serve(monkeypatch, {"/v1/auth/device/code": [(429, payload)]})
    assert _run(monkeypatch, capsys, "login") == (1, "", RATE_LIMITED)
    assert not store.credentials_path().exists()


def test_login_429_while_polling(monkeypatch, capsys):
    # v1.1 item 15: 429 is not slow_down -- polling stops.
    opener = _serve(monkeypatch, {"/v1/auth/device/code": [(200, DEVICE_OK)],
                                  "/v1/auth/token": [(429, {"error": "slow_down"})]})
    assert _run(monkeypatch, capsys, "login") == (1, LOGIN_PROMPT, RATE_LIMITED)
    assert opener.paths().count("/v1/auth/token") == 1


def test_login_503_is_network(monkeypatch, capsys):
    # v1.1 item 15: 503 (temporarily_unavailable) stays network.
    _serve(monkeypatch, {"/v1/auth/device/code": [(200, DEVICE_OK)],
                         "/v1/auth/token": [(503, {"error": "temporarily_unavailable"})]})
    assert _run(monkeypatch, capsys, "login") == (1, LOGIN_PROMPT, UNREACHABLE)


def test_login_revokes_the_old_session_at_its_own_api_url(monkeypatch, capsys):
    # v1.1 item 13: the new login uses TRYAII_API_URL; the replaced session's
    # refresh token goes back only to the host that issued it.
    store.save(_creds(api_url="http://old-issuer.example"))
    opener = _serve(monkeypatch, LOGIN_OK_ROUTES)
    assert _run(monkeypatch, capsys, "login")[0] == 0
    assert [c["url"] for c in opener.calls if c["path"] == "/v1/auth/revoke"] == [
        "http://old-issuer.example/v1/auth/revoke"]
    assert all(c["url"].startswith(API) for c in opener.calls
               if c["path"] != "/v1/auth/revoke")
    assert store.load()["api_url"] == API


def test_login_rejects_flags(monkeypatch, capsys):
    code, _, err = _run(monkeypatch, capsys, "login", "--bogus")
    assert code == 2
    assert "--bogus" in err


# --- logout -----------------------------------------------------------------


def test_logout_via_file(monkeypatch, capsys):
    store.save(_creds())
    opener = _serve(monkeypatch, {"/v1/auth/revoke": [(200, {})]})
    assert _run(monkeypatch, capsys, "logout") == (0, "Logged out.\n", "")
    assert not store.credentials_path().exists()
    assert opener.calls[0]["body"] == {"refresh_token": "tair_old", "client_id": "tryaii-cli"}
    assert opener.calls[0]["url"] == f"{API}/v1/auth/revoke"


def test_logout_revoke_failure_is_ignored(monkeypatch, capsys):
    store.save(_creds())
    _serve(monkeypatch, {"/v1/auth/revoke": [NETWORK]})
    assert _run(monkeypatch, capsys, "logout") == (0, "Logged out.\n", "")
    assert not store.credentials_path().exists()


@pytest.mark.parametrize("env_url", [None, "http://elsewhere.example"])
def test_logout_revokes_at_the_stored_api_url(monkeypatch, capsys, env_url):
    # v1.1 item 13: TRYAII_API_URL is for login only; the refresh token goes
    # back to the host that issued it.
    if env_url is None:
        monkeypatch.delenv("TRYAII_API_URL")
    else:
        monkeypatch.setenv("TRYAII_API_URL", env_url)
    store.save(_creds(api_url="http://stored.example"))
    opener = _serve(monkeypatch, {"/v1/auth/revoke": [(200, {})]})
    assert _run(monkeypatch, capsys, "logout") == (0, "Logged out.\n", "")
    assert [c["url"] for c in opener.calls] == ["http://stored.example/v1/auth/revoke"]


def test_logout_not_logged_in(monkeypatch, capsys):
    opener = _serve(monkeypatch, {})
    assert _run(monkeypatch, capsys, "logout") == (0, "Not logged in.\n", "")
    assert opener.calls == []


def test_logout_ignores_tryaii_token(monkeypatch, capsys):
    # v1.1 item 13: TRYAII_TOKEN is not supported -- it no longer blocks
    # logout; the file session is revoked and removed as usual.
    monkeypatch.setenv("TRYAII_TOKEN", "tair_env")
    store.save(_creds())
    opener = _serve(monkeypatch, {"/v1/auth/revoke": [(200, {})]})
    assert _run(monkeypatch, capsys, "logout") == (0, "Logged out.\n", "")
    assert [c["body"]["refresh_token"] for c in opener.calls] == ["tair_old"]
    assert not store.credentials_path().exists()


def test_logout_cannot_delete_reports_and_exits_1(monkeypatch, capsys):
    # v1.1 item 19: never print "Logged out." while the file is still there.
    store.save(_creds())

    def locked(path=None):
        raise PermissionError(13, "in use")

    monkeypatch.setattr(store, "delete", locked)
    _serve(monkeypatch, {"/v1/auth/revoke": [(200, {})]})
    assert _run(monkeypatch, capsys, "logout") == (
        1, "", "Could not remove the credentials file.\n")
    assert store.load() == _creds()


def test_logout_credentials_path_is_a_directory(monkeypatch, capsys):
    # Same, with a real undeletable "file" (a non-empty directory).
    path = store.credentials_path()
    path.mkdir(parents=True)
    (path / "x").write_text("x", encoding="utf-8")
    opener = _serve(monkeypatch, {})
    assert _run(monkeypatch, capsys, "logout") == (
        1, "", "Could not remove the credentials file.\n")
    assert opener.calls == []
    assert path.is_dir()


def test_logout_file_vanishing_mid_logout_is_fine(monkeypatch, capsys):
    # v1.1 item 19: ENOENT during delete (a parallel logout) is not an error.
    store.save(_creds())
    real_revoke = flow.revoke

    def revoke_then_vanish(api_url, token, **kw):
        store.credentials_path().unlink()
        return real_revoke(api_url, token, **kw)

    monkeypatch.setattr(flow, "revoke", revoke_then_vanish)
    _serve(monkeypatch, {"/v1/auth/revoke": [(200, {})]})
    assert _run(monkeypatch, capsys, "logout") == (0, "Logged out.\n", "")


# --- whoami -----------------------------------------------------------------


def test_whoami_success(monkeypatch, capsys):
    store.save(_creds())
    opener = _serve(monkeypatch, {"/v1/auth/me": [(200, ME_OK)]})
    assert _run(monkeypatch, capsys, "whoami") == (
        0, "Logged in as a@b.com (A B)\nEntitlements: catalog:full\n" + NO_CATALOG, "")
    assert opener.paths() == ["/v1/auth/me"]  # token still fresh: no refresh
    assert opener.calls[0]["headers"]["Authorization"] == "Bearer eyJ.old"


def test_whoami_entitlements_joined_or_none(monkeypatch, capsys):
    store.save(_creds())
    _serve(monkeypatch, {"/v1/auth/me": [(200, {**ME_OK, "entitlements": ["a", "b"]}),
                                         (200, {**ME_OK, "entitlements": []})]})
    assert _run(monkeypatch, capsys, "whoami")[1].endswith("Entitlements: a, b\n" + NO_CATALOG)
    assert _run(monkeypatch, capsys, "whoami")[1].endswith("Entitlements: none\n" + NO_CATALOG)


def test_whoami_json_keeps_server_key_order(monkeypatch, capsys):
    store.save(_creds())
    payload = b'{"session": {"id": "fam1", "created_at": "2026-10-03T12:00:00Z"}, ' \
              b'"user": {"name": "A B", "id": "u1", "email": "a@b.com"}, "entitlements": []}'
    _serve(monkeypatch, {"/v1/auth/me": [(200, payload)]})
    code, out, err = _run(monkeypatch, capsys, "whoami", "--json")
    assert (code, err) == (0, "")
    assert out == (
        '{\n'
        '  "session": {\n'
        '    "id": "fam1",\n'
        '    "created_at": "2026-10-03T12:00:00Z"\n'
        '  },\n'
        '  "user": {\n'
        '    "name": "A B",\n'
        '    "id": "u1",\n'
        '    "email": "a@b.com"\n'
        '  },\n'
        '  "entitlements": []\n'
        '}\n'
    )


def test_whoami_not_logged_in(monkeypatch, capsys):
    opener = _serve(monkeypatch, {})
    assert _run(monkeypatch, capsys, "whoami") == (1, "", "Not logged in. Run: tryaii login\n")
    assert opener.calls == []


def test_whoami_refresh_rotation_is_persisted(monkeypatch, capsys):
    # Access token expires within 60 s of NOW -> refresh first.
    store.save(_creds(access_expires_at="2026-10-03T12:00:30Z"))
    opener = _serve(monkeypatch, {"/v1/auth/token": [(200, TOKEN_OK)],
                                  "/v1/auth/me": [(200, ME_OK)]})
    code, _, err = _run(monkeypatch, capsys, "whoami")
    assert (code, err) == (0, "")
    assert opener.paths() == ["/v1/auth/token", "/v1/auth/me"]
    assert opener.calls[0]["body"]["refresh_token"] == "tair_old"
    assert opener.calls[1]["headers"]["Authorization"] == "Bearer eyJ.access.1"
    saved = store.load()
    assert saved["refresh_token"] == "tair_new"
    assert saved["access_token"] == "eyJ.access.1"
    assert saved["access_expires_at"] == "2026-10-03T13:00:00Z"
    assert saved["created_at"] == "2026-10-01T00:00:00Z"


def test_whoami_invalid_grant_deletes_file(monkeypatch, capsys):
    store.save(_creds(access_expires_at="2026-10-03T11:00:00Z"))
    _serve(monkeypatch, {"/v1/auth/token": [(400, {"error": "invalid_grant"})]})
    assert _run(monkeypatch, capsys, "whoami") == (1, "", ENDED)
    assert not store.credentials_path().exists()


def test_whoami_me_401_deletes_file(monkeypatch, capsys):
    store.save(_creds())
    _serve(monkeypatch, {"/v1/auth/me": [(401, {"error": "invalid_token"})]})
    assert _run(monkeypatch, capsys, "whoami") == (1, "", ENDED)
    assert not store.credentials_path().exists()


@pytest.mark.parametrize("routes,expired", [
    ({"/v1/auth/me": [NETWORK]}, False),
    ({"/v1/auth/token": [NETWORK]}, True),
    ({"/v1/auth/me": [(500, b"oops")]}, False),
])
def test_whoami_network(monkeypatch, capsys, routes, expired):
    creds = _creds(access_expires_at="2026-10-03T11:00:00Z") if expired else _creds()
    store.save(creds)
    _serve(monkeypatch, routes)
    assert _run(monkeypatch, capsys, "whoami") == (1, "", UNREACHABLE)
    assert store.load() == creds  # network failure never deletes the session


def test_whoami_ignores_tryaii_token_without_a_file(monkeypatch, capsys):
    # v1.1 item 13: TRYAII_TOKEN is ignored -- no file means not logged in.
    monkeypatch.setenv("TRYAII_TOKEN", "tair_env")
    opener = _serve(monkeypatch, {"/v1/auth/token": [(200, TOKEN_OK)],
                                  "/v1/auth/me": [(200, ME_OK)]})
    assert _run(monkeypatch, capsys, "whoami") == (1, "", "Not logged in. Run: tryaii login\n")
    assert opener.calls == []


def test_whoami_ignores_tryaii_token_with_a_file(monkeypatch, capsys):
    # v1.1 item 13: the file session is used; the env value is never sent.
    monkeypatch.setenv("TRYAII_TOKEN", "tair_env")
    store.save(_creds(access_expires_at="2026-10-03T11:00:00Z"))
    opener = _serve(monkeypatch, {"/v1/auth/token": [(200, TOKEN_OK)],
                                  "/v1/auth/me": [(200, ME_OK)]})
    assert _run(monkeypatch, capsys, "whoami")[0] == 0
    assert opener.calls[0]["body"]["refresh_token"] == "tair_old"
    assert all("tair_env" not in json.dumps(c["body"]) for c in opener.calls)
    assert store.load()["refresh_token"] == "tair_new"


@pytest.mark.parametrize("expired", [False, True])
def test_whoami_uses_the_stored_api_url_not_the_env(monkeypatch, capsys, expired):
    # v1.1 item 13: refresh and /me go to the issuing host even when
    # TRYAII_API_URL points elsewhere (it is set to API by the fixture).
    stored = "http://issuer.example"
    access = "2026-10-03T11:00:00Z" if expired else "2026-10-03T13:00:00Z"
    store.save(_creds(api_url=stored, access_expires_at=access))
    opener = _serve(monkeypatch, {"/v1/auth/token": [(200, TOKEN_OK)],
                                  "/v1/auth/me": [(200, ME_OK)]})
    assert _run(monkeypatch, capsys, "whoami")[0] == 0
    assert all(c["url"].startswith(stored + "/") for c in opener.calls)
    assert opener.paths() == (["/v1/auth/token"] if expired else []) + ["/v1/auth/me"]
    assert store.load()["api_url"] == stored


def test_whoami_rejected_by_env_host_never_deletes_the_file(monkeypatch, capsys):
    # The review case: TRYAII_API_URL at a host that would reject the token
    # must not even be contacted, let alone cause the file to be deleted.
    store.save(_creds(api_url="http://issuer.example"))
    opener = _serve(monkeypatch, {"/v1/auth/me": [NETWORK]})
    assert _run(monkeypatch, capsys, "whoami") == (
        1, "", "Could not reach http://issuer.example. Check your connection and try again.\n")
    assert [c["url"] for c in opener.calls] == ["http://issuer.example/v1/auth/me"]
    assert store.load() is not None


@pytest.mark.parametrize("routes,expired,code", [
    ({"/v1/auth/token": [(400, {"error": "invalid_request"})]}, True, "invalid_request"),
    ({"/v1/auth/token": [(401, {"error": "invalid_client"})]}, True, "invalid_client"),
    ({"/v1/auth/token": [(400, {"error": "unsupported_grant_type"})]}, True,
     "unsupported_grant_type"),
    ({"/v1/auth/me": [(400, {"error": "invalid_request"})]}, False, "invalid_request"),
    ({"/v1/auth/token": [(429, {"error": "slow_down"})]}, True, "rate_limited"),
    ({"/v1/auth/me": [(429, b"Too Many Requests")]}, False, "rate_limited"),
])
def test_whoami_other_errors_keep_the_file(monkeypatch, capsys, routes, expired, code):
    # v1.1 items 14 + 15: only invalid_grant (refresh) or /me 401 end the
    # session; everything else is "Unexpected response", file kept.
    creds = _creds(access_expires_at="2026-10-03T11:00:00Z") if expired else _creds()
    store.save(creds)
    _serve(monkeypatch, routes)
    assert _run(monkeypatch, capsys, "whoami") == (
        1, "", f"Unexpected response from {API}: {code}.\n")
    assert store.load() == creds


def test_whoami_me_401_with_any_code_ends_the_session(monkeypatch, capsys):
    # v1.1 item 14: /me 401 ends the session whatever the error code.
    store.save(_creds())
    _serve(monkeypatch, {"/v1/auth/me": [(401, {"error": "invalid_client"})]})
    assert _run(monkeypatch, capsys, "whoami") == (1, "", ENDED)
    assert not store.credentials_path().exists()


def test_whoami_rejects_bad_flag(monkeypatch, capsys):
    assert _run(monkeypatch, capsys, "whoami", "--bogus")[0] == 2


# --- help -------------------------------------------------------------------


@pytest.mark.parametrize("command", ["login", "logout", "whoami"])
def test_help_pages(monkeypatch, capsys, command):
    assert _run(monkeypatch, capsys, "help", command) == (
        0, cli_main.COMMAND_HELP[command], "")
    assert _run(monkeypatch, capsys, command, "--help")[1] == cli_main.COMMAND_HELP[command]


def test_global_help_lists_auth_commands():
    lines = cli_main.HELP.splitlines()
    i = next(n for n, line in enumerate(lines) if line.startswith("  regenerate "))
    assert lines[i + 1:i + 4] == [
        "  login                 Sign in with your tryaii.com account "
        "(free; unlocks the full model catalog)",
        "  logout                Sign out and remove the stored credentials",
        "  whoami                Show the signed-in account",
    ]


def test_help_topics_list_the_auth_commands():
    # v1.1 item 21.
    text = cli_main.HELP_HELP
    topics = text[text.index("Topics:"):text.index("Examples:")]
    names = [t.strip() for t in topics.split(":", 1)[1].replace("\n", " ").split(",")]
    assert names[-4:] == ["login", "logout", "whoami", "help"]
    assert set(names) == set(cli_main.COMMAND_HELP)


def test_help_texts_do_not_mention_tryaii_token():
    # v1.1 item 13.
    for text in (cli_main.HELP, cli_main.HELP_LOGIN, cli_main.HELP_LOGOUT,
                 cli_main.HELP_WHOAMI, cli_main.HELP_HELP):
        assert "TRYAII_TOKEN" not in text


# --- end to end against the in-memory fake server ---------------------------


@pytest.fixture
def fake_server():
    from tests.fake_auth_server import start_server

    servers = []

    def _start(**options):
        server, state, url = start_server(**options)
        servers.append(server)
        return state, url

    yield _start
    for server in servers:
        server.shutdown()
        server.server_close()


def test_end_to_end_with_fake_server(monkeypatch, capsys, fake_server):
    state, url = fake_server(approve_after=2, access_expires_in=0)
    monkeypatch.setenv("TRYAII_API_URL", url)

    code, out, err = _run(monkeypatch, capsys, "login")
    assert (code, err) == (0, "")
    # No --catalog-dir on the fake: 404 no_live_release -> the failure line.
    assert out.endswith("Logged in as dev@example.com.\n" + LOGIN_DL_FAILED)
    first = store.load()["refresh_token"]

    # access_expires_in=0 -> whoami refreshes, rotation persisted, /me works.
    code, out, err = _run(monkeypatch, capsys, "whoami")
    assert (code, out, err) == (
        0, "Logged in as dev@example.com (Dev User)\nEntitlements: catalog:full\n"
        + NO_CATALOG, "")
    assert store.load()["refresh_token"] != first

    assert _run(monkeypatch, capsys, "logout") == (0, "Logged out.\n", "")
    assert state.revoked_families
    assert _run(monkeypatch, capsys, "whoami") == (1, "", "Not logged in. Run: tryaii login\n")


def test_end_to_end_denied_with_fake_server(monkeypatch, capsys, fake_server):
    _, url = fake_server(outcome="deny")
    monkeypatch.setenv("TRYAII_API_URL", url)
    code, _, err = _run(monkeypatch, capsys, "login")
    assert (code, err) == (1, "Login denied in the browser.\n")


def test_auth_commands_write_utf8_whatever_the_console_encoding(tmp_path, fake_server):
    # v1.1 item 20: a non-cp1252 email used to crash login AFTER the
    # credentials were saved. Force a cp1252 stdout on every platform.
    import os
    import subprocess
    from pathlib import Path

    email, name = "dév.測試@例子.example", "Zoë 測試"
    _, url = fake_server(approve_after=1, email=email, name=name)
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("TRYAII_")}
    env.update(TRYAII_NO_BANNER="1", TRYAII_DRE_DATA_DIR=str(tmp_path / "data"),
               TRYAII_API_URL=url, PYTHONIOENCODING="cp1252", PYTHONUTF8="0")
    pkg = Path(__file__).resolve().parents[1]

    def run(*argv):
        p = subprocess.run([sys.executable, "-m", "tryaii.cli.main", *argv], cwd=str(pkg),
                           env=env, capture_output=True, timeout=60)
        return p.returncode, p.stdout.decode("utf-8").replace("\r\n", "\n"), p.stderr

    code, out, err = run("login")
    assert (code, err) == (0, b"")
    assert out.endswith(f"Logged in as {email}.\n" + LOGIN_DL_FAILED)
    code, out, err = run("whoami")
    assert (code, err) == (0, b"")
    assert out == f"Logged in as {email} ({name})\nEntitlements: catalog:full\n" + NO_CATALOG
    code, out, err = run("whoami", "--json")
    assert code == 0 and json.loads(out)["user"]["email"] == email
    code, out, err = run("login")  # "Already logged in as <email>." line
    assert code == 0
    assert out.startswith(f"Already logged in as {email}. Signing in again")
    assert run("logout")[:2] == (0, "Logged out.\n")


def test_fake_server_cli_prints_port_first(tmp_path):
    import subprocess
    import urllib.request
    from pathlib import Path

    script = Path(__file__).with_name("fake_auth_server.py")
    proc = subprocess.Popen([sys.executable, str(script), "--port", "0",
                             "--outcome", "approve"],
                            stdout=subprocess.PIPE, text=True)
    try:
        port = int(proc.stdout.readline().strip())
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
            assert json.loads(r.read()) == {"ok": True}
    finally:
        proc.kill()
        proc.wait()
