"""Remote (dashboard) config: the long poll and how the wrapper applies it
(shared/monitor/API.md §3.3, SPEC.md §3.3).

The poller runs synchronously (``poll_once``) through an injected opener;
the wrapper tests use a duck-typed client and an injected ``route_fn`` (no
network, no daemon, no embedding model).
"""

from __future__ import annotations

import io
import json
import logging
import urllib.error
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from tryaii.monitor import remote, store, upload, wrap
from tryaii.monitor import wrapper_openai as wo

KEY = "tryaii_k1a2b3c4d5e6_fixtureSecretfixtureSecretfixtureSecretfixt"
BASE = "https://api.test"
FETCHED = "2026-10-12T09:00:00.000Z"
CONFIG_1 = {"schema": "tryaii.monitor.remote/1", "etag": "e1", "app_id": "app_abcdefghijkl",
            "default_mode": None,
            "processes": {"checkout": {"mode": "managed", "model": "openai/gpt-5-mini"}},
            "updated_at": "2026-10-12T08:00:00.000Z", "updated_by": "google-sub-1"}


class Resp:
    def __init__(self, status, body=None):
        self.status = status
        self._body = b"" if body is None else json.dumps(body).encode("utf-8")
        self.headers = {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Opener:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.urls = []
        self.timeouts = []
        self.auth = []

    def __call__(self, request, timeout=None):
        self.urls.append(request.full_url)
        self.timeouts.append(timeout)
        self.auth.append(request.get_header("Authorization"))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        if status >= 400 or status == 304:
            raise urllib.error.HTTPError(request.full_url, status, "x", {},
                                         io.BytesIO(json.dumps(body or {}).encode("utf-8")))
        return Resp(status, body)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("TRYAII_DRE_DATA_DIR", str(tmp_path))
    for name in ("TRYAII_MONITOR_MODE", "TRYAII_APP", "TRYAII_MONITOR_VECTORS",
                 "TRYAII_API_KEY", "TRYAII_HTTP_FIXTURE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(wo, "_utcnow",
                        lambda: datetime(2026, 10, 12, 9, 0, 0, tzinfo=timezone.utc))
    upload._rejected_logged.clear()
    yield
    upload._rejected_logged.clear()


def poller(*answers, dirs=()):
    opener = Opener(*answers)
    p = remote.RemotePoller(KEY, BASE, opener=opener, now_ts=lambda: FETCHED)
    for d in dirs:
        p.register(d)
    return p, opener


def query(url):
    return {k: v[0] if v else "" for k, v in parse_qs(urlparse(url).query,
                                                       keep_blank_values=True).items()}


# --- the poller --------------------------------------------------------------------

def test_200_writes_remote_json_everywhere_and_tracks_the_etag(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    p, opener = poller((200, CONFIG_1), (304, None), dirs=(a, b))
    assert p.poll_once() == "changed"
    for d in (a, b):
        doc = json.loads((d / "remote.json").read_text(encoding="utf-8"))
        assert list(doc) == list(CONFIG_1) + ["fetched_at"]
        assert doc["fetched_at"] == FETCHED
    assert p.etag == "e1"
    assert p.poll_once() == "unchanged"
    assert query(opener.urls[0]) == {"etag": "", "wait": "25"}
    assert query(opener.urls[1]) == {"etag": "e1", "wait": "25"}
    assert urlparse(opener.urls[0]).path == "/v1/monitor/config"
    assert opener.timeouts == [remote.HTTP_TIMEOUT_S] * 2
    assert opener.auth == [f"Bearer {KEY}"] * 2


def test_304_and_errors_leave_the_cached_config_in_force(tmp_path):
    store.write_remote(tmp_path, CONFIG_1, "2026-10-12T07:00:00.000Z")
    before = (tmp_path / "remote.json").read_bytes()
    p, opener = poller((304, None), (500, {"error": "internal"}),
                       urllib.error.URLError("down"),
                       (200, {"schema": "something/else"}), dirs=(tmp_path,))
    assert p.etag == "e1"                       # picked up from the cached file
    assert [p.poll_once() for _ in range(4)] == ["unchanged", "error", "error", "error"]
    assert (tmp_path / "remote.json").read_bytes() == before
    assert query(opener.urls[0])["etag"] == "e1"


def test_401_stops_polling_and_logs_one_line(tmp_path, caplog):
    p, opener = poller((401, {"error": "invalid_key"}), dirs=(tmp_path,))
    with caplog.at_level(logging.WARNING, logger="tryaii.monitor"):
        assert p.poll_once() == "disabled"
        assert p.poll_once() == "disabled"
    assert len(opener.urls) == 1
    assert sum("was rejected" in r.getMessage() for r in caplog.records) == 1
    assert KEY not in caplog.text


def test_malformed_key_never_polls(tmp_path):
    opener = Opener((200, CONFIG_1))
    p = remote.RemotePoller("not-a-key", BASE, opener=opener)
    assert p.disabled and p.poll_once() == "disabled"
    assert opener.urls == []


def test_etag_is_url_encoded():
    assert remote.config_url("https://x/", "a b/c") == (
        "https://x/v1/monitor/config?etag=a%20b%2Fc&wait=25")


def test_env_switch():
    assert remote.remote_enabled({}) is True
    assert remote.remote_enabled({"TRYAII_MONITOR_REMOTE": "0"}) is False


# --- the wrapper applies remote.json on the next call -----------------------------

MESSAGES = [{"role": "user", "content": "Summarize this order"}]
SCORES = [{"modelId": "openai/gpt-5-mini", "qualityScore": 0.80},
          {"modelId": "openai/gpt-5.5", "qualityScore": 0.82}]


class FakeCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(dict(kwargs))

        class R:
            model = kwargs.get("model")
            usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        return R()


class FakeClient:
    def __init__(self, base_url="https://api.openai.com/v1"):
        self.base_url = base_url

        class Chat:
            completions = FakeCompletions()
        self.chat = Chat()


def route_fn(text):
    return SCORES, None, None


def test_remote_beats_args_and_config_env_beats_remote(monkeypatch):
    client = FakeClient()
    monitor = wrap(client, app="shop", process="checkout", mode="off", route_fn=route_fn)
    store.write_config(monitor.dir, {
        "schema": "tryaii.monitor.config/1", "source": "local", "default_mode": "off",
        "processes": {"checkout": {"mode": "dynamic", "providers": None}}})
    store.write_remote(monitor.dir, CONFIG_1, FETCHED)
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert client.chat.completions.calls[-1]["model"] == "gpt-5-mini"
    monkeypatch.setenv("TRYAII_MONITOR_MODE", "off")
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert client.chat.completions.calls[-1]["model"] == "gpt-5.5"
    events = store.read_recent_events(monitor.dir, 10)
    assert [e["mode"] for e in events] == ["managed", "off"]
    assert events[0]["route_status"] == "ok"


def test_remote_change_applies_after_the_mtime_check(monkeypatch):
    client = FakeClient()
    monitor = wrap(client, app="shop", process="checkout", route_fn=route_fn)
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert client.chat.completions.calls[-1]["model"] == "gpt-5.5"
    store.write_remote(monitor.dir, CONFIG_1, FETCHED)
    # Force the 5 s mtime re-check on the next call.
    monitor._remote._checked_at -= wo.DOC_RECHECK_S + 1
    monitor._remote._mtime = -1.0
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert client.chat.completions.calls[-1]["model"] == "gpt-5-mini"


def test_a_broken_remote_json_means_local_behaviour():
    client = FakeClient()
    monitor = wrap(client, app="shop", process="checkout", mode="off", route_fn=route_fn)
    monitor.dir.mkdir(parents=True, exist_ok=True)
    (monitor.dir / "remote.json").write_text("{not json", encoding="utf-8")
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert client.chat.completions.calls[-1]["model"] == "gpt-5.5"


# --- TS-296: the first calls honour the dashboard ---------------------------------

def test_a_remote_json_written_in_process_applies_on_the_very_next_call():
    # Regression (TS-296): the long poll wrote remote.json 3.5 s after the
    # first call, but the next call still used the old mode because the
    # cached copy was only re-checked every 5 s. No forcing here.
    client = FakeClient()
    monitor = wrap(client, app="shop", process="checkout", route_fn=route_fn)
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert client.chat.completions.calls[-1]["model"] == "gpt-5.5"
    p, _ = poller((200, CONFIG_1), dirs=(monitor.dir,))
    assert p.poll_once() == "changed"
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert client.chat.completions.calls[-1]["model"] == "gpt-5-mini"
    events = store.read_recent_events(monitor.dir, 10)
    assert [e["mode"] for e in events] == ["off", "managed"]


def test_a_same_mtime_rewrite_is_still_picked_up():
    # Coarse mtime (Windows / FAT, or a write within the same tick) must
    # not hide an in-process write.
    client = FakeClient()
    monitor = wrap(client, app="shop", process="checkout", route_fn=route_fn)
    other = dict(CONFIG_1, etag="e0", processes={"checkout": {"mode": "off", "model": None}})
    store.write_remote(monitor.dir, other, FETCHED)
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert client.chat.completions.calls[-1]["model"] == "gpt-5.5"
    path = monitor.dir / "remote.json"
    stat = path.stat()
    store.write_remote(monitor.dir, CONFIG_1, FETCHED)
    import os
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert client.chat.completions.calls[-1]["model"] == "gpt-5-mini"


class GatedOpener(Opener):
    """An opener whose first request blocks until ``release`` is set."""

    def __init__(self, *answers, hold_s=0.0):
        super().__init__(*answers)
        import threading

        self.release = threading.Event()
        self.hold_s = hold_s

    def __call__(self, request, timeout=None):
        if len(self.urls) == 0:
            self.release.wait(self.hold_s)
        return super().__call__(request, timeout)


def _live_poller(monitor, opener):
    p = remote.RemotePoller(KEY, BASE, opener=opener, now_ts=lambda: FETCHED)
    p.register(monitor.dir)
    monitor.transport._remote = p
    monitor.transport._remote_settled = False
    monitor.transport._remote_deadline = None
    p.start()
    return p


def test_the_first_call_waits_for_the_first_answer():
    import time as _time

    client = FakeClient()
    monitor = wrap(client, app="shop", process="checkout", route_fn=route_fn)
    opener = GatedOpener((200, CONFIG_1), (304, None), hold_s=0.15)
    p = _live_poller(monitor, opener)
    try:
        t0 = _time.monotonic()
        client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
        spent = _time.monotonic() - t0
        assert client.chat.completions.calls[-1]["model"] == "gpt-5-mini"
        assert spent < 0.5 + 0.3
        assert query(opener.urls[0]) == {"etag": "", "wait": "0"}
        assert monitor.transport.remote_pending() is False
    finally:
        p.stop()
    events = store.read_recent_events(monitor.dir, 10)
    assert [e["mode"] for e in events] == ["managed"]


def test_the_first_call_wait_is_bounded_and_happens_once():
    import time as _time

    client = FakeClient()
    monitor = wrap(client, app="shop", process="checkout", route_fn=route_fn)
    opener = GatedOpener((200, CONFIG_1), hold_s=10.0)   # a hanging server
    p = _live_poller(monitor, opener)
    try:
        t0 = _time.monotonic()
        client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
        first = _time.monotonic() - t0
        t1 = _time.monotonic()
        client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
        second = _time.monotonic() - t1
        assert 0.4 <= first < 0.5 + 0.4
        assert second < 0.2
        assert [c["model"] for c in client.chat.completions.calls] == ["gpt-5.5", "gpt-5.5"]
        assert monitor.transport.remote_pending() is False
    finally:
        p.stop()
        opener.release.set()


def test_no_first_call_wait_when_the_env_pins_the_mode(monkeypatch):
    import time as _time

    monkeypatch.setenv("TRYAII_MONITOR_MODE", "off")
    client = FakeClient()
    monitor = wrap(client, app="shop", process="checkout", route_fn=route_fn)
    opener = GatedOpener((200, CONFIG_1), hold_s=10.0)
    p = _live_poller(monitor, opener)
    try:
        t0 = _time.monotonic()
        client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
        assert _time.monotonic() - t0 < 0.3
    finally:
        p.stop()
        opener.release.set()


def test_the_async_first_call_waits_off_the_event_loop():
    import asyncio

    class AsyncCompletions:
        def __init__(self):
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(dict(kwargs))

            class R:
                model = kwargs.get("model")
                usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
            return R()

    class AsyncChat:
        completions = AsyncCompletions()

    class AsyncClient:
        base_url = "https://api.openai.com/v1"
        chat = AsyncChat()

    client = AsyncClient()
    monitor = wrap(client, app="shop", process="checkout", route_fn=route_fn)
    opener = GatedOpener((200, CONFIG_1), (304, None), hold_s=0.15)
    p = _live_poller(monitor, opener)
    ticks = []

    async def ticker():
        for _ in range(5):
            ticks.append(1)
            await asyncio.sleep(0.02)

    async def main():
        t = asyncio.create_task(ticker())
        await client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
        await t

    try:
        asyncio.run(main())
    finally:
        p.stop()
    assert client.chat.completions.calls[-1]["model"] == "gpt-5-mini"
    assert len(ticks) == 5


def test_poller_first_answer_is_set_by_any_outcome(tmp_path):
    p, _ = poller(urllib.error.URLError("down"), dirs=(tmp_path,))
    assert not p.first_answer.is_set()
    p.start()
    assert p.first_answer.wait(2.0)
    p.stop()
    bad = remote.RemotePoller("not-a-key", BASE, opener=Opener((200, CONFIG_1)))
    assert bad.first_answer.is_set()
