"""`tryaii apps` (shared/monitor/SPEC.md §7, fixture suite `apps`) and the
HTTP helper behind it (SPEC.md §6).

The goldens run the real CLI in a fresh temp data dir through the
generator's own `run_apps_case`, with every HTTP answer canned via the
TRYAII_HTTP_FIXTURE seam (no network). The unit tests drive
`tryaii.monitor.api.request` through an injected opener.
"""

from __future__ import annotations

import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest

from tryaii.monitor import api

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "shared" / "monitor" / "fixtures"
GENERATOR = REPO_ROOT / "scripts" / "gen-monitor-fixtures.py"
KEY = "tryaii_k1a2b3c4d5e6_fixtureSecretfixtureSecretfixtureSecretfixt"

needs_fixtures = pytest.mark.skipif(
    not (FIXTURES / "apps" / "cases.json").exists() or not GENERATOR.exists(),
    reason="shared/monitor/fixtures/apps not present (package-only checkout)",
)


def _generator():
    spec = importlib.util.spec_from_file_location("gen_monitor_fixtures", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cases() -> list:
    path = FIXTURES / "apps" / "cases.json"
    if not path.exists():
        return []
    doc = json.loads(path.read_text(encoding="utf-8"))
    return [pytest.param(c, id=c["name"]) for c in doc["cases"]]


@needs_fixtures
@pytest.mark.parametrize("case", _cases())
def test_apps_case_matches_golden(case):
    expected = case["expected"]
    if expected is None:
        pytest.skip("expected block not frozen yet")
    proc, requests = _generator().run_apps_case(case["input"])
    stdout = proc.stdout.decode("utf-8").replace("\r\n", "\n")
    stderr = proc.stderr.decode("utf-8").replace("\r\n", "\n")
    golden = (FIXTURES / "apps" / expected["stdout_golden"]).read_text(encoding="utf-8")
    assert proc.returncode == expected["exit_code"], stderr
    assert stdout == golden
    assert (stderr or None) == expected["stderr"]
    assert requests == expected["requests"]


@needs_fixtures
def test_request_log_never_holds_a_credential():
    """The request log (SPEC §6.3) names the auth kind, never the secret."""
    text = (FIXTURES / "apps" / "cases.json").read_text(encoding="utf-8")
    doc = json.loads(text)
    for case in doc["cases"]:
        for request in (case["expected"] or {}).get("requests", []):
            assert set(request) == {"method", "path", "query", "auth", "body"}
            assert request["auth"] in (None, "bearer", "app_key")
            assert "fixtureSecret" not in json.dumps(request)
            assert "eyJ.fixture" not in json.dumps(request)


@needs_fixtures
def test_help_topic_and_verb_pages():
    from tryaii.cli.main import APPS_VERB_HELP, HELP_APPS

    gen = _generator()
    proc, _ = gen.run_apps_case({"argv": ["help", "apps"]})
    assert proc.returncode == 0
    assert proc.stdout.decode("utf-8").replace("\r\n", "\n") == HELP_APPS
    for verb, text in APPS_VERB_HELP.items():
        proc, requests = gen.run_apps_case({"argv": ["apps", verb, "--help"]})
        assert proc.returncode == 0, verb
        assert proc.stdout.decode("utf-8").replace("\r\n", "\n") == text
        assert requests == []


def test_help_lists_apps_everywhere():
    from tryaii.cli.main import COMMAND_HELP, HELP, HELP_HELP

    assert "  apps <verb>" in HELP
    assert " apps," in HELP_HELP
    assert "apps" in COMMAND_HELP


# --- tryaii.monitor.api -----------------------------------------------------------

class _Resp:
    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Opener:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append((request, timeout))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_request_returns_every_status_and_decodes_json():
    opener = Opener(_Resp(201, b'{"app_id": "app_x"}', {"X-A": "1"}))
    resp = api.request("POST", "https://api.test/v1/apps", {"name": "n"}, bearer="tok",
                       opener=opener)
    assert resp.status == 201 and resp.body == {"app_id": "app_x"}
    assert resp.headers == {"x-a": "1"}
    request, timeout = opener.requests[0]
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer tok"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == {"name": "n"}
    assert timeout == api.TIMEOUT_SECONDS


def test_request_maps_http_errors_and_network_failures():
    err = urllib.error.HTTPError("u", 409, "conflict", {}, io.BytesIO(b'{"error":"key_limit"}'))
    resp = api.request("POST", "https://api.test/x", {}, opener=Opener(err))
    assert resp.status == 409 and resp.error == "key_limit" and resp.detail is None
    with pytest.raises(api.ApiNetworkError):
        api.request("GET", "https://api.test/x", opener=Opener(urllib.error.URLError("down")))
    resp = api.request("DELETE", "https://api.test/x", opener=Opener(_Resp(204)))
    assert resp.status == 204 and resp.body is None


def test_request_sends_raw_body_verbatim():
    opener = Opener(_Resp(202, b"{}"))
    api.request("POST", "https://api.test/x", raw_body=b'{"a":1}', opener=opener)
    assert opener.requests[0][0].data == b'{"a":1}'


def test_default_opener_never_follows_redirects():
    handlers = [type(h).__name__ for h in api._OPENER.handlers]
    assert "_NoRedirect" in handlers


def test_app_key_shape():
    assert api.is_app_key(KEY)
    assert not api.is_app_key("tryaii_SHORT_x")
    assert not api.is_app_key("tai_websitekey")
    assert not api.is_app_key(None)


def test_api_url_and_key_from_env(monkeypatch):
    monkeypatch.delenv("TRYAII_API_URL", raising=False)
    monkeypatch.delenv("TRYAII_API_KEY", raising=False)
    assert api.api_url() == "https://api.tryaii.com"
    assert api.api_key() is None
    monkeypatch.setenv("TRYAII_API_URL", "http://localhost:8080/")
    monkeypatch.setenv("TRYAII_API_KEY", f"  {KEY}  ")
    assert api.api_url() == "http://localhost:8080"
    assert api.api_key() == KEY


def test_fixture_seam_serves_routes_in_order_and_logs(monkeypatch, tmp_path):
    fixture = tmp_path / "http.json"
    log = tmp_path / "log.jsonl"
    fixture.write_text(json.dumps({"routes": {
        "GET /v1/monitor/config": [{"status": 200, "body": {"etag": "1"}},
                                   {"status": 304}],
        "POST /v1/monitor/events": [{"network": True}],
    }}), encoding="utf-8")
    monkeypatch.setenv("TRYAII_HTTP_FIXTURE", str(fixture))
    monkeypatch.setenv("TRYAII_HTTP_FIXTURE_LOG", str(log))
    api.reset_fixture_state()
    try:
        url = "https://api.test/v1/monitor/config?etag=&wait=25"
        assert api.request("GET", url, bearer=KEY).body == {"etag": "1"}
        assert api.request("GET", url, bearer=KEY).status == 304
        assert api.request("GET", url, bearer=KEY).status == 304   # the last one repeats
        with pytest.raises(api.ApiNetworkError):
            api.request("POST", "https://api.test/v1/monitor/events", raw_body=b'{"a":1}',
                        bearer="jwt")
        with pytest.raises(api.ApiNetworkError):
            api.request("GET", "https://api.test/unrouted")
    finally:
        api.reset_fixture_state()
    lines = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert lines[0] == {"method": "GET", "path": "/v1/monitor/config",
                        "query": {"etag": "", "wait": "25"}, "auth": "app_key", "body": None}
    assert lines[3] == {"method": "POST", "path": "/v1/monitor/events", "query": None,
                        "auth": "bearer", "body": {"a": 1}}
    assert len(lines) == 5
