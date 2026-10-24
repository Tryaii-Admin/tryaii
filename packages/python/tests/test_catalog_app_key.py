"""Catalog contract v1.4 (TS-294): an app key alone unlocks the full catalog.

Precedence: a ``tryaii login`` session, else ``TRYAII_API_KEY``, else the
starter catalog. A rejected key falls back to the starter catalog without
raising (``auto``). Mirrors packages/node/tests/catalog-app-key.test.ts.
"""

# The fixtures imported from test_catalog_client are used as test arguments,
# which ruff reports as redefinitions (F811).
# ruff: noqa: F811

from __future__ import annotations

import json

import pytest

from tests.fake_auth_server import FAKE_APP_KEY, FAKE_BAD_APP_KEY
from tests.test_catalog_client import (  # noqa: F401 -- fixtures
    DAY,
    V1,
    V2,
    _env,
    _run,
    bundles,
    catalog_calls,
    login_to,
    server,
)
from tryaii.auth import store
from tryaii.catalog import client

KEY_REJECTED = "TRYAII_API_KEY was not accepted for the full catalog; using the starter catalog.\n"


def use_key(monkeypatch, url, key=FAKE_APP_KEY):
    """A server: only TRYAII_API_KEY (and the API base) in the environment."""
    monkeypatch.setenv("TRYAII_API_KEY", key)
    monkeypatch.setenv("TRYAII_API_URL", url)


def state_json():
    return json.loads(client.state_path().read_text(encoding="utf-8"))


# ------------------------------------------------------------- key only
def test_key_only_machine_gets_the_full_catalog(server, monkeypatch):
    state, url = server()
    use_key(monkeypatch, url)
    assert store.load() is None
    sel = client.select_catalog()
    assert (sel.kind, sel.version, sel.notice, sel.checked, sel.source) == (
        "full", V1, None, True, "app_key")
    (call,) = catalog_calls(state)
    assert call[2]["Authorization"] == f"Bearer {FAKE_APP_KEY}"
    assert call[2]["If-None-Match"] is None
    assert state_json() == {"version": V1, "checked_at": "2026-10-03T12:00:00Z", "auth": "app_key"}
    assert client.catalog_in_use() == "full (app key)"


def test_key_only_full_mode_does_not_need_a_login(server, monkeypatch):
    state, url = server()
    use_key(monkeypatch, url)
    assert client.select_catalog("full").kind == "full"


def test_key_within_24h_uses_the_cache_and_after_24h_sends_if_none_match(server, monkeypatch, _env):
    state, url = server()
    use_key(monkeypatch, url)
    client.select_catalog()
    _env["now"] += DAY - 1
    sel = client.select_catalog()
    assert (sel.kind, sel.checked, sel.source) == ("full", False, "app_key")
    assert len(catalog_calls(state)) == 1
    _env["now"] += 2
    sel = client.select_catalog()
    assert (sel.kind, sel.checked) == ("full", True)
    assert catalog_calls(state)[-1][2]["If-None-Match"] == f'"{V1}"'


def test_key_picks_up_a_new_release(server, bundles, monkeypatch, _env):
    state, url = server()
    use_key(monkeypatch, url)
    client.select_catalog()
    state.catalog_dir = str(bundles[V2])
    _env["now"] += DAY + 1
    assert client.select_catalog().version == V2
    assert state_json()["auth"] == "app_key"


# ------------------------------------------------------------- login wins
def test_login_wins_over_the_key(server, monkeypatch):
    state, url = server()
    creds = login_to(state, url)
    use_key(monkeypatch, url)
    sel = client.select_catalog()
    assert (sel.kind, sel.source) == ("full", "login")
    (call,) = catalog_calls(state)
    assert call[2]["Authorization"] == f"Bearer {creds['access_token']}"
    # A login's state keeps the pre-v1.4 bytes (no "auth").
    assert state_json() == {"version": V1, "checked_at": "2026-10-03T12:00:00Z"}
    assert client.catalog_in_use() == "full (login)"


def test_login_session_end_still_raises_even_with_a_key(server, monkeypatch):
    """A login session that ends is not silently replaced by the key (rule 2)."""
    state, url = server()
    login_to(state, url)
    use_key(monkeypatch, url)
    state.catalog_mode = "401"
    with pytest.raises(client.SessionEndedError):
        client.select_catalog()
    assert store.load() is None


def test_switching_from_key_to_login_checks_again_at_once(server, monkeypatch):
    state, url = server()
    use_key(monkeypatch, url)
    client.select_catalog()
    login_to(state, url)
    sel = client.select_catalog()  # inside 24 h, but the last check was the key's
    assert (sel.source, sel.checked) == ("login", True)
    assert len(catalog_calls(state)) == 2
    assert "auth" not in state_json()


def test_switching_from_login_to_key_checks_again_at_once(server, monkeypatch):
    state, url = server()
    login_to(state, url)
    client.select_catalog()
    store.delete()  # e.g. the credentials file was removed by hand
    use_key(monkeypatch, url)
    sel = client.select_catalog()
    assert (sel.source, sel.checked) == ("app_key", True)
    assert catalog_calls(state)[-1][2]["Authorization"] == f"Bearer {FAKE_APP_KEY}"


# ------------------------------------------------------------- bad key
def test_rejected_key_falls_back_to_the_starter_without_raising(server, monkeypatch):
    state, url = server()
    use_key(monkeypatch, url, FAKE_BAD_APP_KEY)
    sel = client.select_catalog()
    assert (sel.kind, sel.notice, sel.source) == ("starter", client.NOTICE_KEY_REJECTED, None)
    assert len(catalog_calls(state)) == 1
    assert not client.state_path().exists()
    assert client.catalog_in_use() == "starter"


def test_revoked_key_drops_the_cached_full_catalog(server, monkeypatch, _env):
    state, url = server()
    use_key(monkeypatch, url)
    assert client.select_catalog().kind == "full"
    state.app_keys.clear()  # revoked
    _env["now"] += DAY + 1
    sel = client.select_catalog()
    assert (sel.kind, sel.notice) == ("starter", client.NOTICE_KEY_REJECTED)
    assert not client.full_dir().exists() and not client.state_path().exists()


def test_rejected_key_in_full_mode_is_login_required(server, monkeypatch):
    state, url = server()
    use_key(monkeypatch, url, FAKE_BAD_APP_KEY)
    with pytest.raises(client.LoginRequiredError):
        client.select_catalog("full")


def test_malformed_key_is_never_sent(server, monkeypatch):
    state, url = server()
    use_key(monkeypatch, url, "not-a-key")
    sel = client.select_catalog()
    assert (sel.kind, sel.notice) == ("starter", client.NOTICE_KEY_REJECTED)
    assert catalog_calls(state) == []


def test_key_without_entitlement_is_the_no_access_starter(server, monkeypatch):
    state, url = server(entitlements=())
    use_key(monkeypatch, url)
    sel = client.select_catalog()
    assert (sel.kind, sel.notice) == ("starter", client.NOTICE_NO_ENTITLEMENT)
    assert state_json() == {
        "version": None, "checked_at": "2026-10-03T12:00:00Z", "auth": "app_key"}


@pytest.mark.parametrize("mode", ["404", "429", "500", "corrupt"])
def test_key_download_failure_is_the_download_notice(server, monkeypatch, mode):
    state, url = server(catalog_mode=mode)
    use_key(monkeypatch, url)
    sel = client.select_catalog()
    assert (sel.kind, sel.notice, sel.failed) == ("starter", client.NOTICE_DOWNLOAD_FAILED, True)


def test_key_network_failure_never_raises(monkeypatch):
    use_key(monkeypatch, "http://127.0.0.1:9")
    sel = client.select_catalog()
    assert (sel.kind, sel.notice) == ("starter", client.NOTICE_DOWNLOAD_FAILED)


# ------------------------------------------------------------- library + CLI
def test_library_memo_is_keyed_by_the_key(server, monkeypatch):
    state, url = server()
    use_key(monkeypatch, url, FAKE_BAD_APP_KEY)
    assert client.selected_bundle().kind == "starter"
    use_key(monkeypatch, url)
    assert client.selected_bundle().kind == "full"


def test_cli_models_with_a_rejected_key_prints_the_key_line(server, monkeypatch, capsys):
    state, url = server()
    use_key(monkeypatch, url, FAKE_BAD_APP_KEY)
    code, out, err = _run(monkeypatch, capsys, "models")
    assert (code, err) == (0, KEY_REJECTED)
    assert "Available Models (46)" in out


def test_cli_models_with_a_key_has_no_nudge(server, monkeypatch, capsys):
    state, url = server()
    use_key(monkeypatch, url)
    code, _out, err = _run(monkeypatch, capsys, "models")
    assert (code, err) == (0, "")


def test_monitor_status_catalog_line(server, monkeypatch):
    from tryaii.cli.monitor import _catalog_line

    assert _catalog_line() == "catalog: starter"
    state, url = server()
    use_key(monkeypatch, url)
    client.select_catalog()
    assert _catalog_line() == "catalog: full (app key)"
    login_to(state, url)
    client.select_catalog()
    assert _catalog_line() == "catalog: full (login)"
