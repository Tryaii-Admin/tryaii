"""Catalog client: docs/catalog/CONTRACT-catalog-v1.md section 5.

One test (or a small group) per rule of section 5 -- cache layout, which
catalog (auto / starter / full), the daily check and every server outcome,
the nudge, the CLI lines, logout / whoami / login, plus the daemon and
centroid-cache keying that make the selection stick. Driven against the
in-process fake server (tests/fake_auth_server.py) with
GET /v1/catalog/live serving small but valid full bundles.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.fake_auth_server import make_full_bundle, start_server
from tryaii.auth import flow, store
from tryaii.catalog import client
from tryaii.catalog.bundle import BUNDLE_DATA_FILES, starter_bundle
from tryaii.cli import main as cli_main

NOW = 1791028800.0  # 2026-10-03T12:00:00Z
DAY = 24 * 3600
V1 = "2026.10.04.1"
V2 = "2026.10.05.1"
DEAD = "http://127.0.0.1:9"  # nothing listens on the discard port

NUDGE = ("Routing on the starter catalog (45 models). Log in for free to use the "
         "full catalog (322 models): tryaii login\n")


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("TRYAII_DRE_DATA_DIR", str(tmp_path / "data"))
    # A stored session never talks to TRYAII_API_URL (auth v1.1 item 13).
    monkeypatch.setenv("TRYAII_API_URL", DEAD)
    monkeypatch.delenv("TRYAII_NO_BANNER", raising=False)
    monkeypatch.delenv("TRYAII_TOKEN", raising=False)
    monkeypatch.setenv("TRYAII_NO_DAEMON", "1")
    clock = {"now": NOW}
    monkeypatch.setattr(flow, "clock", lambda: clock["now"])
    monkeypatch.setattr(flow, "sleep", lambda s: None)
    client.reset_cache()
    yield clock
    client.reset_cache()


@pytest.fixture(scope="module")
def bundles(tmp_path_factory):
    base = tmp_path_factory.mktemp("bundles")
    return {V1: make_full_bundle(base / V1, V1), V2: make_full_bundle(base / V2, V2)}


@pytest.fixture
def server(bundles):
    servers = []

    def _start(**options):
        options.setdefault("catalog_dir", str(bundles[V1]))
        srv, state, url = start_server(**options)
        servers.append(srv)
        return state, url

    yield _start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def login_to(state, url, *, access_expires_in=3600):
    """Store credentials for a fresh session on the fake server."""
    state.access_expires_in = access_expires_in
    _, token = state._issue(f"{state._next():032x}")
    creds = flow.credentials_from_token(url, token, flow.clock())
    store.save(creds)
    return creds


def catalog_calls(state):
    return [r for r in state.requests if r[1] == "/v1/catalog/live"]


def cache_versions():
    base = client.full_dir()
    return sorted(p.name for p in base.iterdir()) if base.is_dir() else []


# ------------------------------------------------------------- which catalog
def test_not_logged_in_is_starter_with_the_nudge_notice_and_no_network(server):
    state, _ = server()
    sel = client.select_catalog()
    assert sel.kind == "starter" and sel.notice == client.NOTICE_NOT_LOGGED_IN
    assert not sel.checked
    assert catalog_calls(state) == []


def test_starter_mode_never_touches_the_network(server):
    state, url = server()
    login_to(state, url)
    sel = client.select_catalog("starter")
    assert (sel.kind, sel.notice) == ("starter", None)
    assert catalog_calls(state) == []


def test_full_mode_requires_a_login():
    with pytest.raises(client.LoginRequiredError) as exc:
        client.select_catalog("full")
    assert str(exc.value) == "Not logged in. Run: tryaii login"


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        client.select_catalog("private")


def test_first_check_downloads_verifies_and_caches(server, bundles):
    state, url = server()
    login_to(state, url)
    sel = client.select_catalog()
    assert (sel.kind, sel.version, sel.notice, sel.checked) == ("full", V1, None, True)
    # one request, to the STORED api_url, no If-None-Match (nothing cached),
    # bearer = the stored access token, gzip accepted.
    (call,) = catalog_calls(state)
    assert call[2]["If-None-Match"] is None
    assert call[2]["Authorization"] == f"Bearer {store.load()['access_token']}"
    assert "gzip" in call[2]["Accept-Encoding"]
    assert call[2]["User-Agent"].startswith("tryaii/")
    # catalog/full/<version>/ holds the six files; data files byte-identical.
    vdir = client.full_dir() / V1
    assert sorted(p.name for p in vdir.iterdir()) == sorted(
        [*BUNDLE_DATA_FILES, "manifest.json"])
    for name in BUNDLE_DATA_FILES:
        assert (vdir / name).read_bytes() == (bundles[V1] / name).read_bytes()
    manifest = json.loads((vdir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == json.loads((bundles[V1] / "manifest.json").read_text(encoding="utf-8"))
    assert json.loads(client.state_path().read_text(encoding="utf-8")) == {
        "version": V1, "checked_at": "2026-10-03T12:00:00Z"}
    assert sel.bundle.directory == vdir


def test_within_24h_the_cache_is_used_without_asking(server, _env):
    state, url = server()
    login_to(state, url)
    client.select_catalog()
    _env["now"] = NOW + DAY - 1
    sel = client.select_catalog()
    assert (sel.kind, sel.version, sel.checked) == ("full", V1, False)
    assert len(catalog_calls(state)) == 1


def test_after_24h_a_304_touches_checked_at(server, _env):
    state, url = server()
    login_to(state, url, access_expires_in=10 * DAY)
    client.select_catalog()
    _env["now"] = NOW + DAY
    sel = client.select_catalog()
    assert (sel.kind, sel.version, sel.checked) == ("full", V1, True)
    calls = catalog_calls(state)
    assert len(calls) == 2 and calls[1][2]["If-None-Match"] == f'"{V1}"'
    assert json.loads(client.state_path().read_text(encoding="utf-8"))["checked_at"] == \
        store.iso_z(NOW + DAY)
    assert cache_versions() == [V1]


def test_a_new_release_replaces_the_old_version_dir(server, bundles, _env):
    state, url = server()
    login_to(state, url, access_expires_in=10 * DAY)
    client.select_catalog()
    state.catalog_dir = str(bundles[V2])
    _env["now"] = NOW + DAY + 5
    sel = client.select_catalog()
    assert sel.version == V2
    assert cache_versions() == [V2]  # only the newest version dir is kept
    assert json.loads(client.state_path().read_text(encoding="utf-8"))["version"] == V2


def test_checked_at_in_the_future_counts_as_stale(server, _env):
    state, url = server()
    login_to(state, url)
    client.select_catalog()
    client.write_state(V1, NOW + 3 * DAY)
    client.select_catalog()
    assert len(catalog_calls(state)) == 2


def test_missing_version_dir_triggers_a_check(server):
    state, url = server()
    login_to(state, url)
    client.select_catalog()
    import shutil

    shutil.rmtree(client.full_dir())
    sel = client.select_catalog()
    assert sel.version == V1 and len(catalog_calls(state)) == 2


def test_expired_access_token_is_refreshed_first_and_persisted(server):
    state, url = server()
    old = login_to(state, url, access_expires_in=0)
    sel = client.select_catalog()
    assert sel.kind == "full"
    paths = [p for _, p, _ in state.requests]
    assert paths == ["/v1/auth/token", "/v1/catalog/live"]
    new = store.load()
    assert new["refresh_token"] != old["refresh_token"]
    assert catalog_calls(state)[0][2]["Authorization"] == f"Bearer {new['access_token']}"


def test_stored_api_url_wins_over_tryaii_api_url(server, monkeypatch):
    state, url = server()
    login_to(state, url)
    monkeypatch.setenv("TRYAII_API_URL", DEAD)
    assert client.select_catalog().kind == "full"
    assert len(catalog_calls(state)) == 1


# ------------------------------------------------------------- session ended
def _assert_everything_gone():
    assert not store.credentials_path().exists()
    assert not client.full_dir().exists()
    assert not client.state_path().exists()


def test_refresh_invalid_grant_ends_the_session(server, _env):
    state, url = server()
    login_to(state, url, access_expires_in=0)
    state.refresh_outcome = "ok"
    client.select_catalog()  # cache it
    state.refresh_outcome = "invalid_grant"
    _env["now"] = NOW + DAY
    with pytest.raises(client.SessionEndedError) as exc:
        client.select_catalog()
    assert str(exc.value) == "Your session has ended. Run: tryaii login"
    _assert_everything_gone()


def test_catalog_401_ends_the_session(server, _env):
    state, url = server()
    login_to(state, url, access_expires_in=10 * DAY)
    client.select_catalog()
    state.catalog_mode = "401"
    _env["now"] = NOW + DAY
    with pytest.raises(client.SessionEndedError):
        client.select_catalog()
    _assert_everything_gone()
    # no silent fallback inside the session; afterwards: simply logged out.
    assert client.select_catalog().notice == client.NOTICE_NOT_LOGGED_IN


@pytest.mark.parametrize("refresh_outcome", ["rate_limited", "invalid_request"])
def test_other_refresh_failures_keep_the_session(server, refresh_outcome):
    state, url = server(refresh_outcome=refresh_outcome)
    login_to(state, url, access_expires_in=0)
    sel = client.select_catalog()
    assert (sel.kind, sel.notice) == ("starter", client.NOTICE_DOWNLOAD_FAILED)
    assert store.load() is not None


# ------------------------------------------------------------- 403
def test_403_is_starter_and_is_remembered_for_24h(server, _env):
    state, url = server(entitlements=())
    login_to(state, url)
    sel = client.select_catalog()
    assert (sel.kind, sel.notice) == ("starter", client.NOTICE_NO_ENTITLEMENT)
    assert json.loads(client.state_path().read_text(encoding="utf-8"))["version"] is None
    _env["now"] = NOW + 60
    sel = client.select_catalog()
    assert (sel.kind, sel.notice, sel.checked) == ("starter", client.NOTICE_NO_ENTITLEMENT, False)
    assert len(catalog_calls(state)) == 1
    assert store.load() is not None  # 403 is not a session end


def test_403_drops_a_previously_cached_full_catalog(server, _env):
    state, url = server()
    login_to(state, url, access_expires_in=10 * DAY)
    client.select_catalog()
    state.catalog_mode = "403"
    _env["now"] = NOW + DAY
    assert client.select_catalog().notice == client.NOTICE_NO_ENTITLEMENT
    assert cache_versions() == []


# ------------------------------------------------------------- failures
FAILURES = ["404", "429", "500", "corrupt", "network"]


def _break(state, mode, monkeypatch):
    if mode == "network":
        from tryaii.auth import transport

        def refuse(request, timeout=None):
            raise OSError("connection refused")

        monkeypatch.setattr(transport, "urlopen", refuse)
    else:
        state.catalog_mode = mode


@pytest.mark.parametrize("mode", FAILURES)
def test_failure_without_cache_is_starter_with_the_download_notice(server, mode, monkeypatch):
    state, url = server()
    login_to(state, url)
    _break(state, mode, monkeypatch)
    sel = client.select_catalog()
    assert (sel.kind, sel.notice) == ("starter", client.NOTICE_DOWNLOAD_FAILED)
    # failures never touch state.json: the next use retries.
    assert not client.state_path().exists()
    assert store.load() is not None


@pytest.mark.parametrize("mode", FAILURES + ["schema"])
def test_failure_with_cache_uses_the_cache_silently(server, bundles, mode, monkeypatch, _env):
    state, url = server()
    login_to(state, url, access_expires_in=10 * DAY)
    client.select_catalog()
    before = client.state_path().read_bytes()
    state.catalog_dir = str(bundles[V2])  # a newer release (not a 304) ...
    _break(state, mode, monkeypatch)  # ... that fails to arrive intact
    _env["now"] = NOW + DAY
    sel = client.select_catalog()
    assert (sel.kind, sel.version, sel.notice) == ("full", V1, None)
    assert client.state_path().read_bytes() == before


def test_no_live_release_without_a_catalog_dir_is_a_failure(server):
    state, url = server(catalog_dir=None)
    login_to(state, url)
    assert client.select_catalog().notice == client.NOTICE_DOWNLOAD_FAILED


def test_schema_too_new_without_cache(server):
    state, url = server(catalog_mode="schema")
    login_to(state, url)
    sel = client.select_catalog()
    assert (sel.kind, sel.notice) == ("starter", client.NOTICE_SCHEMA_TOO_NEW)
    assert client.notice_message(sel.notice) == (
        "The full catalog needs a newer tryaii version; using the starter catalog.")


def test_hash_mismatch_is_never_written(server):
    state, url = server(catalog_mode="corrupt")
    login_to(state, url)
    client.select_catalog()
    assert cache_versions() == []


def test_the_check_uses_the_auth_transport_timeout(server, monkeypatch):
    from tryaii.auth import transport

    state, url = server()
    login_to(state, url)
    seen = []
    real = transport.urlopen

    def spy(request, timeout=None):
        seen.append((request.full_url, timeout))
        return real(request, timeout=timeout)

    monkeypatch.setattr(transport, "urlopen", spy)
    client.select_catalog()
    assert seen == [(f"{url}/v1/catalog/live", 10.0)]


def test_writes_go_through_the_atomic_helper(server, monkeypatch):
    state, url = server()
    login_to(state, url)
    written = []
    real = store.write_atomic

    def spy(path, data, **kw):
        written.append(Path(path).name)
        return real(path, data, **kw)

    monkeypatch.setattr(store, "write_atomic", spy)
    client.select_catalog()
    assert sorted(written) == sorted([*BUNDLE_DATA_FILES, "manifest.json", "state.json"])


# ------------------------------------------------------------- counts / nudge
def test_routable_count_skips_free_variants(bundles):
    from tryaii.catalog import load_bundle

    full = load_bundle(bundles[V1])
    assert full.counts["models"] == 46
    assert client.routable_count(full) == 45
    assert client.routable_count(starter_bundle()) == 45


def test_starter_manifest_full_counts_are_routable():
    # Contract section 1: full_counts.models = the full catalog's ROUTABLE count.
    assert starter_bundle().full_counts == {"models": 322, "benchmarks": 33}


def test_nudge_once_per_local_day(_env):
    lines = []
    sel = client.select_catalog()
    assert client.maybe_nudge(sel, lines.append) is True
    assert client.maybe_nudge(sel, lines.append) is False
    assert lines == [NUDGE]
    assert json.loads(client.nudge_path().read_text(encoding="utf-8")) == {
        "last_shown": "2026-10-03" if _local(NOW) == "2026-10-03" else _local(NOW)}
    _env["now"] = NOW + DAY
    assert client.maybe_nudge(sel, lines.append) is True
    assert len(lines) == 2


def _local(epoch):
    import datetime as dt

    return dt.date.fromtimestamp(epoch).isoformat()


def test_nudge_suppressed_by_tryaii_no_banner(monkeypatch):
    monkeypatch.setenv("TRYAII_NO_BANNER", "1")
    lines = []
    assert client.maybe_nudge(client.select_catalog(), lines.append) is False
    assert lines == [] and not client.nudge_path().exists()


def test_nudge_only_when_not_logged_in():
    lines = []
    for notice in (None, client.NOTICE_NO_ENTITLEMENT, client.NOTICE_DOWNLOAD_FAILED):
        sel = client.CatalogSelection(starter_bundle(), notice)
        assert client.maybe_nudge(sel, lines.append) is False
    assert lines == []


# ------------------------------------------------------------- library
def test_router_catalog_option(server):
    from tryaii import Router

    assert Router(catalog="starter").bundle.kind == "starter"
    assert Router().bundle.kind == "starter"  # auto, not logged in
    with pytest.raises(client.LoginRequiredError):
        Router(catalog="full")
    with pytest.raises(ValueError):
        Router(catalog="everything")
    state, url = server()
    login_to(state, url)
    client.reset_cache()
    assert Router(catalog="full").bundle.kind == "full"
    assert Router().bundle.version == V1
    # an explicit bundle wins over the catalog option
    assert Router(bundle=starter_bundle(), catalog="full").bundle.kind == "starter"


def test_library_checks_once_per_process(server):
    from tryaii import ModelRegistry, Router
    from tryaii.benchmarks.registry import BenchmarkRegistry

    state, url = server()
    login_to(state, url)
    Router()
    assert len(ModelRegistry.default().all_models) == 45
    BenchmarkRegistry.default()
    Router(catalog="full")
    # auto and full are memoized separately; each checked the server at most once.
    assert len(catalog_calls(state)) <= 2


def test_library_session_ended_raises(server):
    from tryaii import Router, SessionEndedError

    state, url = server(refresh_outcome="invalid_grant")
    login_to(state, url, access_expires_in=0)
    with pytest.raises(SessionEndedError):
        Router()


def test_library_never_prints(server, capsys):
    from tryaii import Router

    Router()
    state, url = server(entitlements=())
    login_to(state, url)
    client.reset_cache()
    Router()
    assert capsys.readouterr() == ("", "")


# ------------------------------------------------------------- CLI
def _run(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["tryaii", "--no-banner", *argv])
    code = 0
    try:
        cli_main.cli()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_models_nudges_once_a_day(monkeypatch, capsys):
    code, out, err = _run(monkeypatch, capsys, "models")
    assert (code, err) == (0, NUDGE)
    assert out.startswith("\nAvailable Models (45):")
    assert _run(monkeypatch, capsys, "models")[2] == ""


def test_cli_nudge_respects_tryaii_no_banner(monkeypatch, capsys):
    monkeypatch.setenv("TRYAII_NO_BANNER", "1")
    assert _run(monkeypatch, capsys, "models")[2] == ""


def test_cli_benchmarks_does_not_nudge(monkeypatch, capsys):
    assert _run(monkeypatch, capsys, "benchmarks")[2] == ""


def test_cli_models_lists_the_full_catalog_when_logged_in(server, monkeypatch, capsys):
    state, url = server()
    login_to(state, url)
    code, out, err = _run(monkeypatch, capsys, "models", "--json")
    assert (code, err) == (0, "")
    ids = [m["model_id"] for m in json.loads(out)]
    assert len(ids) == 45 and not any(i.endswith(":free") for i in ids)


@pytest.mark.parametrize("options, line", [
    ({"entitlements": ()},
     "This account does not have access to the full catalog; using the starter catalog.\n"),
    ({"catalog_mode": "500"},
     "Could not download the full catalog; using the starter catalog for now.\n"),
    ({"catalog_mode": "schema"},
     "The full catalog needs a newer tryaii version; using the starter catalog.\n"),
])
def test_cli_fallback_lines(server, monkeypatch, capsys, options, line):
    state, url = server(**options)
    login_to(state, url)
    code, out, err = _run(monkeypatch, capsys, "models")
    assert (code, err) == (0, line)
    assert "Available Models (45)" in out


def test_cli_session_ended(server, monkeypatch, capsys):
    state, url = server(catalog_mode="401")
    login_to(state, url)
    assert _run(monkeypatch, capsys, "models") == (
        1, "", "Your session has ended. Run: tryaii login\n")
    _assert_everything_gone()


def test_cli_login_downloads(server, monkeypatch, capsys):
    state, url = server(approve_after=1)
    monkeypatch.setenv("TRYAII_API_URL", url)
    code, out, err = _run(monkeypatch, capsys, "login")
    assert (code, err) == (0, "")
    assert out.endswith("Logged in as dev@example.com.\nDownloaded the full catalog (45 models).\n")
    assert cache_versions() == [V1]


@pytest.mark.parametrize("options", [{"entitlements": ()}, {"catalog_mode": "500"},
                                     {"catalog_dir": None}, {"catalog_mode": "schema"}])
def test_cli_login_download_failure_keeps_exit_0(server, monkeypatch, capsys, options):
    state, url = server(approve_after=1, **options)
    monkeypatch.setenv("TRYAII_API_URL", url)
    code, out, err = _run(monkeypatch, capsys, "login")
    assert (code, err) == (0, "")
    assert out.endswith("Logged in as dev@example.com.\n"
                        "Could not download the full catalog now; it will be fetched on next use.\n")
    assert store.load() is not None


def test_cli_whoami_catalog_line(server, monkeypatch, capsys):
    state, url = server()
    login_to(state, url)
    assert _run(monkeypatch, capsys, "whoami")[1].endswith("Catalog: not downloaded yet\n")
    client.select_catalog()
    code, out, err = _run(monkeypatch, capsys, "whoami")
    assert (code, err) == (0, "")
    assert out == ("Logged in as dev@example.com (Dev User)\nEntitlements: catalog:full\n"
                   f"Catalog: full, release {V1} (45 models)\n")
    # --json unchanged: the server's JSON only
    assert "Catalog" not in _run(monkeypatch, capsys, "whoami", "--json")[1]


def test_cli_whoami_session_ended_drops_the_cache(server, monkeypatch, capsys):
    state, url = server()
    login_to(state, url)
    client.select_catalog()
    state.me_outcome = "401"
    assert _run(monkeypatch, capsys, "whoami")[0] == 1
    _assert_everything_gone()


@pytest.mark.parametrize("revoke_ok", [True, False])
def test_cli_logout_deletes_the_cache_and_state(server, monkeypatch, capsys, revoke_ok):
    state, url = server()
    login_to(state, url)
    client.select_catalog()
    store.write_atomic(client.nudge_path(), b'{"last_shown": "2026-10-03"}')
    if not revoke_ok:
        from tryaii.auth import transport

        real = transport.urlopen

        def no_revoke(request, timeout=None):
            if request.full_url.endswith("/v1/auth/revoke"):
                raise OSError("down")
            return real(request, timeout=timeout)

        monkeypatch.setattr(transport, "urlopen", no_revoke)
    assert _run(monkeypatch, capsys, "logout") == (0, "Logged out.\n", "")
    _assert_everything_gone()
    assert client.nudge_path().exists()  # the nudge memory stays


def test_help_text_matches_the_contract():
    assert ("  login                 Sign in with your tryaii.com account "
            "(free; unlocks the full model catalog)\n") in cli_main.HELP
    assert ("TRYAII_DRE_DATA_DIR).\n\nAfter signing in, the full model catalog is "
            "downloaded and kept up to date\nautomatically (checked at most once a day).\n"
            "\nEnvironment:") in cli_main.HELP_LOGIN
    for text in (cli_main.HELP, cli_main.HELP_LOGIN):
        assert "private" not in text and "public" not in text
        assert "`" not in text and "${" not in text


# ------------------------------------------------------------- daemon / centroids
def test_daemon_is_keyed_by_catalog(tmp_path):
    from tryaii import daemon
    from tryaii.catalog import load_bundle
    from tryaii.config import TryaiiDreConfig

    config = TryaiiDreConfig(data_dir=tmp_path)
    starter = starter_bundle()
    assert daemon.catalog_key(starter) == f"starter:{starter.version}"
    assert daemon.catalog_spec(starter) == "starter"
    assert daemon.bundle_from_spec("starter") is starter
    assert daemon.bundle_from_spec("") is starter
    full = make_full_bundle(tmp_path / "full" / V1, V1)
    loaded = load_bundle(full)
    assert daemon.catalog_spec(loaded) == str(full)
    assert daemon.bundle_from_spec(str(full)).version == V1
    # A state file for another catalog never counts as live for this one.
    daemon.write_state(config, {"runtime": "python", "embeddingModel": config.embedding_model,
                                "catalog": "starter:x", "host": "127.0.0.1", "port": 9,
                                "token": "t", "pid": 0})
    assert daemon._live_state(config, f"full:{V1}") is None


def test_daemon_serves_the_catalog_it_was_handed(tmp_path):
    import threading
    import time

    from tryaii import daemon, server
    from tryaii.config import TryaiiDreConfig

    class FakeRouter:
        def route(self, prompt, priorities=None, top_k=5):  # pragma: no cover
            raise AssertionError

    full = make_full_bundle(tmp_path / V1, V1)
    config = TryaiiDreConfig(data_dir=tmp_path / "d")
    t = threading.Thread(target=server.serve, kwargs=dict(
        config=config, idle_timeout=0, router=FakeRouter(), log=lambda m: None,
        catalog=str(full)), daemon=True)
    t.start()
    state = None
    for _ in range(100):
        state = daemon._live_state(config, f"full:{V1}")
        if state:
            break
        time.sleep(0.05)
    try:
        assert state is not None and state["catalog"] == f"full:{V1}"
        assert daemon.status(config)["catalog"] == f"full:{V1}"
        assert daemon._live_state(config, f"starter:{starter_bundle().version}") is None
    finally:
        daemon.stop(config)
        t.join(timeout=5)


def test_centroid_cache_is_keyed_by_catalog(tmp_path):
    from tryaii.catalog import load_bundle
    from tryaii.config import TryaiiDreConfig

    config = TryaiiDreConfig(data_dir=tmp_path, embedding_model="org/other-model")
    starter = starter_bundle()
    full = load_bundle(make_full_bundle(tmp_path / V1, V1))
    a = config.centroid_file_for(starter)
    b = config.centroid_file_for(full)
    assert a != b
    assert a.name == f"centroids_org__other-model__starter-{starter.version}.json"
    assert b.name == f"centroids_org__other-model__full-{V1}.json"


def test_centroid_cache_prunes_older_versions_of_the_same_kind(tmp_path):
    import numpy as np

    from tryaii.catalog import load_bundle
    from tryaii.centroids.loader import CentroidLoader
    from tryaii.config import TryaiiDreConfig

    class Provider:
        model_name = "fake-model"
        dimension = 3

    config = TryaiiDreConfig(data_dir=tmp_path, embedding_model="fake-model")
    config.ensure_dirs()
    old = config.centroids_dir / "centroids_fake-model__full-2026.10.01.1.json"
    other_kind = config.centroids_dir / f"centroids_fake-model__starter-{starter_bundle().version}.json"
    old.write_text("{}", encoding="utf-8")
    other_kind.write_text("{}", encoding="utf-8")
    full = load_bundle(make_full_bundle(tmp_path / V1, V1))
    loader = CentroidLoader(config=config, embedding_provider=Provider(), bundle=full)
    loader._save({name: np.zeros(3, dtype=np.float32) for name in full.benchmark_names})
    assert loader.cache_path.exists()
    assert not old.exists()
    assert other_kind.exists()


# ------------------------------------------------------------- review fixes
# Finding 1: a 200 whose hashes match but whose shape is wrong, or whose
# `files` values are not strings, is a failed download -- never a crash.
@pytest.mark.parametrize("mode", ["malformed", "badtype"])
def test_malformed_bundle_without_cache_is_a_failed_download(server, mode):
    from tryaii import Router

    state, url = server(catalog_mode=mode)
    login_to(state, url)
    sel = client.select_catalog()
    assert (sel.kind, sel.notice, sel.failed) == ("starter", client.NOTICE_DOWNLOAD_FAILED, True)
    assert cache_versions() == [] and not client.state_path().exists()
    client.reset_cache()
    assert Router().bundle.kind == "starter"  # the library does not raise either


@pytest.mark.parametrize("mode", ["malformed", "badtype"])
def test_malformed_bundle_with_cache_keeps_the_cache(server, bundles, mode, _env):
    state, url = server()
    login_to(state, url, access_expires_in=10 * DAY)
    client.select_catalog()
    state.catalog_dir = str(bundles[V2])
    state.catalog_mode = mode
    _env["now"] = NOW + DAY
    sel = client.select_catalog()
    assert (sel.kind, sel.version, sel.notice) == ("full", V1, None)
    assert cache_versions() == [V1]


def test_bundle_from_texts_rejects_non_text_values_and_bad_shapes():
    from tryaii.catalog.bundle import BundleError, bundle_from_texts, sha256_hex

    starter = starter_bundle()
    texts = {name: (starter.directory / name).read_text(encoding="utf-8")
             for name in BUNDLE_DATA_FILES}
    manifest = json.loads(json.dumps(starter.manifest))
    # 10**12 must be rejected by type, not turned into a 1 TB bytes object.
    for bad in (10 ** 12, {"a": 1}, None):
        with pytest.raises(BundleError):
            bundle_from_texts(manifest, {**texts, "models.json": bad})
    manifest["files"]["benchmarks.json"] = sha256_hex(b"{}")
    with pytest.raises(BundleError, match="malformed"):
        bundle_from_texts(manifest, {**texts, "benchmarks.json": "{}"})


# Finding 2: the per-process memo retries failed selections after a short
# backoff instead of keeping them for 24 h.
def test_library_memo_retries_a_failed_check_after_the_backoff(server, monkeypatch):
    mono = {"t": 1000.0}
    monkeypatch.setattr(client, "_monotonic", lambda: mono["t"])
    state, url = server(catalog_mode="500")
    login_to(state, url)
    assert client.selected_bundle().kind == "starter"
    state.catalog_mode = "ok"
    mono["t"] += client.FAILURE_RETRY_SECONDS - 1
    assert client.selected_bundle().kind == "starter"  # backoff: no hammering
    assert len(catalog_calls(state)) == 1
    mono["t"] += 2
    assert client.selected_bundle().version == V1  # retried
    assert len(catalog_calls(state)) == 2
    mono["t"] += client.CHECK_INTERVAL_SECONDS - 10
    assert client.selected_bundle().version == V1  # a success is kept for 24 h
    assert len(catalog_calls(state)) == 2


def test_library_memo_keeps_final_answers_for_24h(server, monkeypatch):
    mono = {"t": 1000.0}
    monkeypatch.setattr(client, "_monotonic", lambda: mono["t"])
    state, url = server(entitlements=())
    login_to(state, url)
    assert client.selected_bundle().kind == "starter"  # no_entitlement
    mono["t"] += client.FAILURE_RETRY_SECONDS * 10
    assert client.selected_bundle().kind == "starter"
    assert len(catalog_calls(state)) == 1


# Finding 3: `diagnose check` selects the catalog once and passes it down.
INVENTORY = (Path(__file__).resolve().parents[3] / "shared" / "diagnose" / "fixtures"
             / "corpus" / "inventory-mixed.json")


def _diagnose(monkeypatch, capsys, tmp_path, *extra):
    return _run(monkeypatch, capsys, "diagnose", "check", str(INVENTORY), "--json",
                "--run-id", "r", "--now", "2026-01-01T00:00:00Z",
                "--out-dir", str(tmp_path / "out"), *extra)


def test_cli_diagnose_check_session_ended_is_the_contract_line(server, monkeypatch, capsys,
                                                               tmp_path):
    state, url = server(catalog_mode="401")
    login_to(state, url)
    assert _diagnose(monkeypatch, capsys, tmp_path) == (
        1, "", "Your session has ended. Run: tryaii login\n")
    _assert_everything_gone()


def test_cli_diagnose_check_selects_the_catalog_once(server, monkeypatch, capsys, tmp_path):
    import tryaii.diagnose as diagnose_pkg

    seen = []
    real = diagnose_pkg.analyze_inventory

    def spy(data, opts=None, classify_fn=None, registry=None):
        seen.append(registry)
        return real(data, opts, classify_fn, registry)

    monkeypatch.setattr(diagnose_pkg, "analyze_inventory", spy)
    state, url = server(catalog_mode="500")
    login_to(state, url)
    code, out, err = _diagnose(monkeypatch, capsys, tmp_path)
    assert (code, err) == (
        0, "Could not download the full catalog; using the starter catalog for now.\n")
    assert json.loads(out)["summary"]["site_count"] >= 1
    assert len(catalog_calls(state)) == 1  # no second selection inside the engine
    assert len(seen) == 1 and seen[0] is not None


# Finding 4: replacing a daemon on another catalog never kills one that a
# concurrent CLI just started on the wanted catalog; a daemon that dies
# mid-request falls back to in-process routing.
def test_daemon_stop_keeps_a_daemon_on_the_wanted_catalog(tmp_path):
    from tryaii import daemon
    from tryaii.config import TryaiiDreConfig

    config = TryaiiDreConfig(data_dir=tmp_path)
    daemon.write_state(config, {"runtime": "python", "embeddingModel": config.embedding_model,
                                "catalog": f"full:{V1}", "host": "127.0.0.1", "port": 9,
                                "token": "t", "pid": 0})
    assert daemon.stop(config, keep_catalog=f"full:{V1}") is False
    assert daemon.read_state(config)["catalog"] == f"full:{V1}"
    daemon.stop(config, keep_catalog="starter:x")
    assert daemon.read_state(config) is None


def test_ensure_daemon_does_not_replace_a_concurrently_started_daemon(tmp_path, monkeypatch):
    from tryaii import daemon
    from tryaii.catalog import load_bundle
    from tryaii.config import TryaiiDreConfig

    config = TryaiiDreConfig(data_dir=tmp_path / "d")
    full = load_bundle(make_full_bundle(tmp_path / V1, V1))
    key = daemon.catalog_key(full)
    other = {"runtime": "python", "embeddingModel": config.embedding_model,
             "catalog": "starter:x", "host": "127.0.0.1", "port": 9, "token": "t", "pid": 0}
    right = dict(other, catalog=key)
    calls = []

    def live(cfg, catalog=None):
        calls.append(catalog)
        if len(calls) == 1:
            return None  # first look: nothing on our catalog ...
        if len(calls) == 2:
            # ... a daemon on another catalog -- and meanwhile a concurrent
            # CLI replaces it with one on ours:
            daemon.write_state(cfg, right)
            return other
        return right if catalog in (None, key) else None

    def must_not(*args, **kwargs):
        raise AssertionError("must not stop or spawn")

    monkeypatch.setattr(daemon, "_live_state", live)
    monkeypatch.setattr(daemon, "_request", must_not)
    monkeypatch.setattr(daemon, "_spawn_serve", must_not)
    assert daemon.ensure_daemon(config, bundle=full) == right
    assert daemon.read_state(config)["catalog"] == key


def test_daemon_route_failure_falls_back_to_in_process(monkeypatch):
    from tryaii import daemon

    tries = []

    def dead(state, prompt, priorities, top_k):
        tries.append(prompt)
        raise ConnectionResetError("daemon died")

    monkeypatch.setattr(daemon, "route", dead)
    monkeypatch.setattr(cli_main, "_inprocess_route_fn",
                        lambda config, bundle: (lambda p, pr, k: ("inprocess", p, bundle)))
    bundle = starter_bundle()
    route_fn = cli_main._daemon_route_fn({"host": "127.0.0.1", "port": 9, "token": "t"},
                                         None, bundle)
    assert route_fn("a", None, 1) == ("inprocess", "a", bundle)
    assert route_fn("b", None, 1) == ("inprocess", "b", bundle)
    assert tries == ["a"]  # the dead daemon is not retried for every prompt


# Finding 5: versions and checked_at must match exactly (no trailing "\n").
def test_version_with_a_trailing_newline_is_rejected(server, tmp_path):
    bad = make_full_bundle(tmp_path / "nl", V1 + "\n")
    state, url = server(catalog_dir=str(bad))
    login_to(state, url)
    assert client.select_catalog().notice == client.NOTICE_DOWNLOAD_FAILED
    assert cache_versions() == []
    assert client._VERSION_RE.fullmatch(V1) and not client._VERSION_RE.fullmatch(V1 + "\n")
    # ASCII digits only, like the Node SDK's /\d/
    assert not client._VERSION_RE.fullmatch("\u0662\u0660\u0662\u0666.10.04.1")


def test_checked_at_must_be_exact_iso_z():
    fresh = "2026-10-03T12:00:00Z"
    assert client._check_due({"checked_at": fresh}, NOW) is False
    for bad in (fresh + "\n", "2026-10-3T12:00:00Z", " " + fresh):
        assert client._check_due({"checked_at": bad}, NOW) is True


# Finding 8: logout / session end also drop the centroid caches derived from
# the full catalog.
def test_clear_cache_drops_full_catalog_centroid_caches(server):
    state, url = server()
    login_to(state, url)
    client.select_catalog()
    cdir = store.data_dir() / "centroids"
    cdir.mkdir(parents=True, exist_ok=True)
    full_cache = cdir / f"centroids_org__model__full-{V1}.json"
    starter_cache = cdir / "centroids_org__model__starter-2026.10.03.1.json"
    legacy = cdir / "centroids_org__model.json"
    for path in (full_cache, starter_cache, legacy):
        path.write_text("{}", encoding="utf-8")
    client.clear_cache()
    assert not full_cache.exists()
    assert starter_cache.exists() and legacy.exists()
    assert cache_versions() == [] and not client.state_path().exists()


# ------------------------------------------------------------- signing (section 6)
# Catalog contract section 6: a full catalog is accepted only with a valid
# signature from a trusted key; missing signature / unknown key_id / bad
# signature (and any manifest change after signing) is handled exactly like a
# hash mismatch: cached full catalog if any, else the starter + the
# download-failure line. conftest.py trusts the tests' signing key through
# TRYAII_CATALOG_TRUSTED_KEYS; the fake server's bundles are signed with it.
SIGNATURE_FAILURES = ["unsigned", "badsig", "unknownkey", "tampered"]


def test_a_validly_signed_catalog_is_accepted_and_cached_with_its_signature(server, bundles):
    from tests._catalog_signing import TEST_KEY_ID

    state, url = server()
    login_to(state, url)
    sel = client.select_catalog()
    assert (sel.kind, sel.version, sel.notice) == ("full", V1, None)
    cached = json.loads((client.full_dir() / V1 / "manifest.json").read_text(encoding="utf-8"))
    sent = json.loads((bundles[V1] / "manifest.json").read_text(encoding="utf-8"))
    assert cached["key_id"] == TEST_KEY_ID
    assert cached["signature"] == sent["signature"]  # kept as received


@pytest.mark.parametrize("mode", SIGNATURE_FAILURES)
def test_untrusted_signature_without_cache_is_starter_and_never_written(server, mode):
    state, url = server(catalog_mode=mode)
    login_to(state, url)
    sel = client.select_catalog()
    assert (sel.kind, sel.notice, sel.failed) == ("starter", client.NOTICE_DOWNLOAD_FAILED, True)
    assert client.notice_message(sel.notice) == (
        "Could not download the full catalog; using the starter catalog for now.")
    # verified BEFORE anything is written: no version dir, no state.json
    assert not client.full_dir().exists() or cache_versions() == []
    assert not client.state_path().exists()
    assert store.load() is not None  # the session is kept


@pytest.mark.parametrize("mode", SIGNATURE_FAILURES)
def test_untrusted_signature_with_cache_keeps_the_cache(server, bundles, mode, _env):
    state, url = server()
    login_to(state, url, access_expires_in=10 * DAY)
    client.select_catalog()
    before = client.state_path().read_bytes()
    state.catalog_dir = str(bundles[V2])
    state.catalog_mode = mode
    _env["now"] = NOW + DAY
    sel = client.select_catalog()
    assert (sel.kind, sel.version, sel.notice) == ("full", V1, None)
    assert cache_versions() == [V1]
    assert client.state_path().read_bytes() == before


def test_non_ascii_manifest_is_rejected_even_when_validly_signed(server, tmp_path):
    from tests._catalog_signing import sign_manifest

    bad = make_full_bundle(tmp_path / "nonascii", V1)
    manifest = json.loads((bad / "manifest.json").read_text(encoding="utf-8"))
    manifest["created_at"] = "2026-10-04T00:00:00Z é"
    sign_manifest(manifest)  # the (independent) test signer signs anything
    (bad / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False),
                                       encoding="utf-8")
    state, url = server(catalog_dir=str(bad))
    login_to(state, url)
    sel = client.select_catalog()
    assert (sel.kind, sel.notice) == ("starter", client.NOTICE_DOWNLOAD_FAILED)
    assert cache_versions() == []


def _tamper_cached_manifest(version, change):
    path = client.full_dir() / version / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    change(manifest)
    path.write_text(client.manifest_text(manifest), encoding="utf-8")


@pytest.mark.parametrize("change", [
    lambda m: m.update(signature=None, key_id=None),
    lambda m: m.update(created_at="2026-10-04T00:00:09Z"),
    lambda m: m.update(key_id="test-catalog-other"),
], ids=["unsigned", "tampered", "unknown_key"])
def test_the_cache_is_reverified_when_loaded(server, change, _env):
    state, url = server()
    login_to(state, url, access_expires_in=10 * DAY)
    assert client.select_catalog().kind == "full"
    client.reset_cache()  # a new process
    assert client.cached_full(client.read_state()) is not None
    _tamper_cached_manifest(V1, change)
    client.reset_cache()
    assert client.cached_full(client.read_state()) is None  # treated as absent
    # within 24 h: state names V1 but the cache is unusable -> check now (200)
    calls = len(catalog_calls(state))
    sel = client.select_catalog()
    assert (sel.kind, sel.version, sel.checked) == ("full", V1, True)
    assert len(catalog_calls(state)) == calls + 1
    assert catalog_calls(state)[-1][2]["If-None-Match"] is None  # no trusted cache to offer
    client.reset_cache()
    assert client.cached_full(client.read_state()) is not None  # rewritten, verified


def test_the_cache_is_reverified_against_the_trusted_list_in_effect(server, monkeypatch,
                                                                    tmp_path):
    from tests._catalog_signing import OTHER_KEY_ID, OTHER_SEED, key_entry, write_trusted_keys

    state, url = server()
    login_to(state, url, access_expires_in=10 * DAY)
    assert client.select_catalog().kind == "full"
    client.reset_cache()
    other_only = write_trusted_keys(tmp_path / "other.json", [key_entry(OTHER_KEY_ID, OTHER_SEED)])
    monkeypatch.setenv("TRYAII_CATALOG_TRUSTED_KEYS", str(other_only))
    assert client.cached_full(client.read_state()) is None
    # ... and the library falls back like a failed download (the server's
    # bundle is signed with a key this list does not trust either)
    sel = client.select_catalog()
    assert (sel.kind, sel.notice) == ("starter", client.NOTICE_DOWNLOAD_FAILED)


def test_the_packaged_list_does_not_trust_the_test_key(server, monkeypatch):
    monkeypatch.delenv("TRYAII_CATALOG_TRUSTED_KEYS")
    state, url = server()
    login_to(state, url)
    sel = client.select_catalog()
    assert (sel.kind, sel.notice) == ("starter", client.NOTICE_DOWNLOAD_FAILED)


def test_starter_needs_no_signature_even_with_an_empty_trusted_list(monkeypatch, tmp_path):
    from tests._catalog_signing import write_trusted_keys
    from tryaii import Router

    monkeypatch.setenv("TRYAII_CATALOG_TRUSTED_KEYS",
                       str(write_trusted_keys(tmp_path / "none.json", [])))
    assert starter_bundle().manifest["signature"] is None
    assert client.select_catalog("starter").kind == "starter"
    sel = client.select_catalog()  # auto, not logged in
    assert (sel.kind, sel.notice) == ("starter", client.NOTICE_NOT_LOGGED_IN)
    assert Router(catalog="starter").bundle.kind == "starter"


@pytest.mark.parametrize("mode", SIGNATURE_FAILURES)
def test_cli_untrusted_signature_lines(server, monkeypatch, capsys, mode):
    state, url = server(catalog_mode=mode)
    login_to(state, url)
    code, out, err = _run(monkeypatch, capsys, "models")
    assert (code, err) == (0, "Could not download the full catalog; using the starter catalog "
                              "for now.\n")
    assert "Available Models (45)" in out


@pytest.mark.parametrize("mode", SIGNATURE_FAILURES)
def test_cli_login_with_an_untrusted_signature(server, monkeypatch, capsys, mode):
    state, url = server(approve_after=1, catalog_mode=mode)
    monkeypatch.setenv("TRYAII_API_URL", url)
    code, out, err = _run(monkeypatch, capsys, "login")
    assert (code, err) == (0, "")
    assert out.endswith(
        "Could not download the full catalog now; it will be fetched on next use.\n")
    assert cache_versions() == []


def test_daemon_reverifies_a_cache_dir_but_not_a_callers_own_bundle(server, tmp_path):
    from tryaii import daemon
    from tryaii.catalog.signing import BundleSignatureError

    state, url = server()
    login_to(state, url)
    sel = client.select_catalog()
    spec = daemon.catalog_spec(sel.bundle)
    assert daemon.bundle_from_spec(spec).version == V1  # signed cache: fine
    _tamper_cached_manifest(V1, lambda m: m.update(created_at="2026-10-04T00:00:09Z"))
    with pytest.raises(BundleSignatureError):
        daemon.bundle_from_spec(spec)
    own = make_full_bundle(tmp_path / "own", V1, signed=False)
    assert daemon.bundle_from_spec(str(own)).kind == "full"  # Router(bundle=...) path
