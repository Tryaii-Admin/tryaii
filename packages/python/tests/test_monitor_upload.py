"""The background uploader (shared/monitor/API.md §3.2, SPEC.md §3.4).

Every test drives :class:`tryaii.monitor.upload.Uploader` synchronously
(``run_once``) through an injected opener and a fake monotonic clock: no
thread, no network, no daemon.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error

import pytest

from tryaii.monitor import NOTICE_VERSION, store, upload
from tryaii.monitor.events import build_event

KEY = "tryaii_k1a2b3c4d5e6_fixtureSecretfixtureSecretfixtureSecretfixt"
BASE = "https://api.test"
VALID = {"app_id": "app_abcdefghijkl", "app_name": "shop", "key_id": "k1a2b3c4d5e6",
         "scopes": ["monitor:write"], "expires_at": None}


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
    """Answers by path; each path's list is served in order (last repeats).
    An answer is ``(status, body)`` or an exception instance."""

    def __init__(self, routes):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls = []

    def __call__(self, request, timeout=None):
        from urllib.parse import urlparse

        path = urlparse(request.full_url).path
        self.calls.append({"method": request.get_method(), "path": path,
                           "auth": request.get_header("Authorization"),
                           "data": request.data, "timeout": timeout})
        queue = self.routes[path]
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        if status >= 400:
            import io

            raise urllib.error.HTTPError(request.full_url, status, "err", {},
                                         io.BytesIO(json.dumps(body or {}).encode("utf-8")))
        return Resp(status, body)

    def posts(self):
        return [c for c in self.calls if c["method"] == "POST"]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def event(i, ts="2026-10-12T08:00:00.000Z", **extra):
    e = build_event(ts=ts, app="shop", process="p", call_site=None, provider="openai",
                    model_requested="gpt-5.5", model_resolved="openai/gpt-5.5",
                    model_used="gpt-5.5", mode="off", winner=None, winner_reachable=False,
                    route_status="ok", classification=None, usage=None, est_cost_usd=None,
                    est_cost_winner_usd=None, latency_ms=i, stream=False, error=None,
                    prompt_hash="h", vector=False,
                    event_id=f"00000000-0000-4000-8000-{i:012d}")
    e.update(extra)
    return e


def write_events(directory, events, day="2026-10-12"):
    path = directory / f"events-{day}.jsonl"
    with open(path, "ab") as fh:
        for e in events:
            fh.write(store.dumps_line(e).encode("utf-8"))
    return path


def make(tmp_path, routes, **kw):
    opener = Opener(routes)
    clock = Clock()
    up = upload.Uploader(KEY, BASE, opener=opener, clock=clock,
                         now_ts=lambda: "2026-10-12T09:00:00.000Z", version="0.7.0", **kw)
    up.register(tmp_path)
    return up, opener, clock


@pytest.fixture(autouse=True)
def _reset_log_once():
    upload._rejected_logged.clear()
    yield
    upload._rejected_logged.clear()


# --- happy path -------------------------------------------------------------------

def test_validates_once_then_uploads_the_unread_lines(tmp_path):
    path = write_events(tmp_path, [event(1), event(2)])
    up, opener, clock = make(tmp_path, {
        "/v1/apps/keys/validate": [(200, VALID)],
        "/v1/monitor/events": [(202, {"accepted": 2, "batch_id": "x"})]})
    assert up.run_once() == "sent"
    assert [c["path"] for c in opener.calls] == ["/v1/apps/keys/validate",
                                                  "/v1/monitor/events"]
    post = opener.posts()[0]
    assert post["auth"] == f"Bearer {KEY}"
    body = json.loads(post["data"])
    assert list(body) == ["schema", "batch_id", "sdk", "notice_version", "events"]
    assert body["schema"] == "tryaii.monitor.upload/1"
    assert body["sdk"] == {"lang": "python", "version": "0.7.0"}
    assert body["notice_version"] == NOTICE_VERSION == "2026-10"
    assert [e["event_id"] for e in body["events"]] == [
        "00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"]
    # The stored lines are spliced in verbatim.
    lines = path.read_bytes().splitlines()
    assert post["data"].endswith(b"[" + b",".join(lines) + b"]}")
    cursor = store.read_upload_cursor(tmp_path)
    assert cursor == {"schema": "tryaii.monitor.upload-cursor/1",
                      "files": {path.name: path.stat().st_size},
                      "last_ok": "2026-10-12T09:00:00.000Z", "last_error": None}
    assert up.validated == VALID and up.sent_events == 2

    # At most one batch every 5 s; nothing new afterwards.
    write_events(tmp_path, [event(3)])
    assert up.run_once() == "wait"
    clock.t += upload.INTERVAL_S
    assert up.run_once() == "sent"
    assert len(opener.posts()) == 2
    assert len([c for c in opener.calls if c["path"] == "/v1/apps/keys/validate"]) == 1
    clock.t += upload.INTERVAL_S
    assert up.run_once() == "idle"


def test_local_files_are_never_modified(tmp_path):
    path = write_events(tmp_path, [event(1)])
    before = path.read_bytes()
    up, _, _ = make(tmp_path, {"/v1/apps/keys/validate": [(200, VALID)],
                               "/v1/monitor/events": [(202, {})]})
    up.run_once()
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name, "upload.json"]


def test_batches_hold_at_most_500_events(tmp_path):
    write_events(tmp_path, [event(i) for i in range(501)])
    up, opener, clock = make(tmp_path, {"/v1/apps/keys/validate": [(200, VALID)],
                                        "/v1/monitor/events": [(202, {})]})
    up.run_once()
    clock.t += upload.INTERVAL_S
    up.run_once()
    sizes = [len(json.loads(p["data"])["events"]) for p in opener.posts()]
    assert sizes == [500, 1]


def test_body_stays_under_the_byte_cap(tmp_path):
    write_events(tmp_path, [event(i) for i in range(10)])
    one = len(store.dumps_line(event(0)).encode("utf-8")) - 1
    cursor = store.empty_upload_cursor()
    batch = upload.next_batch(tmp_path, cursor, max_bytes=100 + 2 + one * 3 + 2,
                              head_bytes=100)
    assert len(batch.lines) == 3
    assert batch.end == batch.start + 3 * (one + 1)


def test_one_file_per_batch_oldest_first(tmp_path):
    write_events(tmp_path, [event(1)], day="2026-10-11")
    write_events(tmp_path, [event(2)], day="2026-10-12")
    up, opener, clock = make(tmp_path, {"/v1/apps/keys/validate": [(200, VALID)],
                                        "/v1/monitor/events": [(202, {})]})
    up.run_once()
    clock.t += upload.INTERVAL_S
    up.run_once()
    ids = [json.loads(p["data"])["events"][0]["latency_ms"] for p in opener.posts()]
    assert ids == [1, 2]
    assert list(store.read_upload_cursor(tmp_path)["files"]) == [
        "events-2026-10-11.jsonl", "events-2026-10-12.jsonl"]


# --- what is never sent -----------------------------------------------------------

def test_forbidden_fields_never_leave_the_machine(tmp_path):
    bad = event(2)
    bad["classification"] = {"category": "x", "text": "the prompt"}
    write_events(tmp_path, [event(1), bad, event(3, messages=[{"role": "user"}])])
    up, opener, _ = make(tmp_path, {"/v1/apps/keys/validate": [(200, VALID)],
                                    "/v1/monitor/events": [(202, {})]})
    assert up.run_once() == "sent"
    body = opener.posts()[0]["data"]
    assert b"the prompt" not in body and b'"messages"' not in body
    assert [e["latency_ms"] for e in json.loads(body)["events"]] == [1]
    assert store.read_upload_cursor(tmp_path)["last_error"] == "forbidden_field: text"


@pytest.mark.parametrize("field", upload.FORBIDDEN_FIELDS)
def test_find_forbidden_any_depth(field):
    assert upload.find_forbidden({"a": [{"b": {field: 1}}]}) == field
    assert upload.find_forbidden({"a": [{"b": {"ok": 1}}]}) is None


def test_skips_legacy_malformed_and_partial_lines(tmp_path):
    legacy = event(1)
    del legacy["event_id"]
    path = write_events(tmp_path, [legacy])
    with open(path, "ab") as fh:
        fh.write(b"not json\n")
        fh.write(b'{"schema":"tryaii.monitor.vector/1"}\n')
    write_events(tmp_path, [event(2)])
    with open(path, "ab") as fh:
        fh.write(store.dumps_line(event(3)).encode("utf-8").rstrip(b"\n"))  # mid-write
    up, opener, _ = make(tmp_path, {"/v1/apps/keys/validate": [(200, VALID)],
                                    "/v1/monitor/events": [(202, {})]})
    up.run_once()
    sent = json.loads(opener.posts()[0]["data"])["events"]
    assert [e["latency_ms"] for e in sent] == [2]
    size = path.stat().st_size
    partial = len(store.dumps_line(event(3)).encode("utf-8")) - 1
    assert store.read_upload_cursor(tmp_path)["files"][path.name] == size - partial


def test_a_rewritten_shorter_file_restarts_at_zero(tmp_path):
    path = write_events(tmp_path, [event(1)])
    store.write_upload_cursor(tmp_path, {"files": {path.name: 10_000}})
    up, opener, _ = make(tmp_path, {"/v1/apps/keys/validate": [(200, VALID)],
                                    "/v1/monitor/events": [(202, {})]})
    assert up.run_once() == "sent"
    assert len(json.loads(opener.posts()[0]["data"])["events"]) == 1


# --- failures ---------------------------------------------------------------------

def test_400_skips_the_batch_and_records_the_error(tmp_path):
    path = write_events(tmp_path, [event(1)])
    up, _, clock = make(tmp_path, {
        "/v1/apps/keys/validate": [(200, VALID)],
        "/v1/monitor/events": [(400, {"error": "bad_batch", "detail": "events[0].ts"})]})
    assert up.run_once() == "skipped"
    cursor = store.read_upload_cursor(tmp_path)
    assert cursor["files"][path.name] == path.stat().st_size
    assert cursor["last_error"] == "bad_batch: events[0].ts"
    assert up.backoff == 0


def test_401_on_upload_disables_the_process_and_logs_one_line(tmp_path, caplog):
    write_events(tmp_path, [event(1)])
    up, opener, clock = make(tmp_path, {"/v1/apps/keys/validate": [(200, VALID)],
                                        "/v1/monitor/events": [(401, {"error": "invalid_key"})]})
    with caplog.at_level(logging.WARNING, logger="tryaii.monitor"):
        assert up.run_once() == "rejected"
        clock.t += 1000
        assert up.run_once() == "disabled"
    assert len(opener.posts()) == 1
    assert store.read_upload_cursor(tmp_path)["last_error"] == "invalid_key"
    assert store.read_upload_cursor(tmp_path)["files"] == {}
    lines = [r for r in caplog.records if "TRYAII_API_KEY was rejected" in r.getMessage()]
    assert len(lines) == 1
    assert KEY not in caplog.text


def test_401_on_validate_disables_before_any_upload(tmp_path):
    write_events(tmp_path, [event(1)])
    up, opener, _ = make(tmp_path, {"/v1/apps/keys/validate": [(401, {"error": "invalid_key"})],
                                    "/v1/monitor/events": [(202, {})]})
    assert up.run_once() == "disabled"
    assert opener.posts() == []
    assert up.disabled
    # The status line must say so, not stay at the last "ok" (phase 4 smoke).
    assert store.read_upload_cursor(tmp_path)["last_error"] == "invalid_key"


def test_malformed_key_is_never_sent(tmp_path):
    opener = Opener({})
    up = upload.Uploader("tai_websitekey", BASE, opener=opener)
    up.register(tmp_path)
    assert up.disabled and up.run_once() == "disabled"
    assert opener.calls == []


def test_5xx_and_network_back_off_exponentially_to_5_minutes(tmp_path):
    path = write_events(tmp_path, [event(1)])
    up, opener, clock = make(tmp_path, {
        "/v1/apps/keys/validate": [(200, VALID)],
        "/v1/monitor/events": [(503, {}), urllib.error.URLError("down")]})
    delays = []
    for _ in range(9):
        clock.t = max(clock.t, up.next_at)
        assert up.run_once() == "backoff"
        delays.append(up.backoff)
    assert delays == [5, 10, 20, 40, 80, 160, 300, 300, 300]
    cursor = store.read_upload_cursor(tmp_path)
    assert cursor["files"].get(path.name, 0) == 0
    assert cursor["last_error"] == "network"


def test_429_honours_retry_after(tmp_path):
    write_events(tmp_path, [event(1)])
    up, _, clock = make(tmp_path, {
        "/v1/apps/keys/validate": [(200, VALID)],
        "/v1/monitor/events": [(429, {"error": "rate_limited", "retry_after": 42})]})
    assert up.run_once() == "backoff"
    assert up.next_at == clock.t + 42
    assert store.read_upload_cursor(tmp_path)["last_error"] == "rate_limited"


def test_validate_network_error_retries_later(tmp_path):
    write_events(tmp_path, [event(1)])
    up, opener, clock = make(tmp_path, {
        "/v1/apps/keys/validate": [urllib.error.URLError("down"), (200, VALID)],
        "/v1/monitor/events": [(202, {})]})
    assert up.run_once() == "backoff"
    assert up.run_once() == "wait"
    clock.t = up.next_at
    assert up.run_once() == "sent"


def test_success_clears_the_last_error(tmp_path):
    write_events(tmp_path, [event(1)])
    up, _, clock = make(tmp_path, {"/v1/apps/keys/validate": [(200, VALID)],
                                   "/v1/monitor/events": [(503, {}), (202, {})]})
    up.run_once()
    assert store.read_upload_cursor(tmp_path)["last_error"] == "http 503"
    clock.t = up.next_at
    assert up.run_once() == "sent"
    assert store.read_upload_cursor(tmp_path)["last_error"] is None
    assert up.backoff == 0


def test_run_once_never_raises(tmp_path, monkeypatch):
    write_events(tmp_path, [event(1)])
    up, _, _ = make(tmp_path, {"/v1/apps/keys/validate": [(200, VALID)]})

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(upload, "next_batch", boom)
    assert up.run_once() is None


# --- lock, switches, transport wiring ---------------------------------------------

def test_dir_lock_is_exclusive_and_stale_locks_are_taken_over(tmp_path):
    a, b = upload._DirLock(tmp_path), upload._DirLock(tmp_path)
    assert a.acquire()
    assert not b.acquire()
    old = time.time() - upload.LOCK_STALE_S - 5
    os.utime(a.path, (old, old))
    assert b.acquire()
    b.release()
    assert not a.path.exists()


def test_a_held_lock_skips_the_directory(tmp_path):
    write_events(tmp_path, [event(1)])
    up, opener, _ = make(tmp_path, {"/v1/apps/keys/validate": [(200, VALID)],
                                    "/v1/monitor/events": [(202, {})]})
    held = upload._DirLock(tmp_path)
    assert held.acquire()
    assert up.run_once() == "idle"
    assert opener.posts() == []
    held.release()


def test_env_switch():
    assert upload.upload_enabled({}) is True
    assert upload.upload_enabled({"TRYAII_MONITOR_UPLOAD": "0"}) is False
    assert upload.upload_enabled({"TRYAII_MONITOR_UPLOAD": "1"}) is True


@pytest.mark.parametrize("env,expect_upload,expect_remote", [
    ({}, False, False),
    ({"TRYAII_API_KEY": KEY}, True, True),
    ({"TRYAII_API_KEY": KEY, "TRYAII_MONITOR_UPLOAD": "0"}, False, True),
    ({"TRYAII_API_KEY": KEY, "TRYAII_MONITOR_REMOTE": "0"}, True, False),
])
def test_transport_starts_services_only_with_a_key(monkeypatch, tmp_path, env,
                                                   expect_upload, expect_remote):
    from tryaii.monitor import remote
    from tryaii.monitor.transport import Transport

    for name in ("TRYAII_API_KEY", "TRYAII_MONITOR_UPLOAD", "TRYAII_MONITOR_REMOTE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    started = []
    monkeypatch.setattr(upload.Uploader, "start", lambda self: started.append("upload"))
    monkeypatch.setattr(remote.RemotePoller, "start", lambda self: started.append("remote"))
    transport = Transport()
    transport.register_app(tmp_path)
    transport._start_services()
    assert (transport._uploader is not None) is expect_upload
    assert (transport._remote is not None) is expect_remote
    if expect_upload:
        assert transport._uploader.dirs == [tmp_path]
    assert sorted(started) == sorted(
        (["upload"] if expect_upload else []) + (["remote"] if expect_remote else []))


def test_injected_transports_never_start_services(monkeypatch):
    from tryaii.monitor.transport import Transport

    monkeypatch.setenv("TRYAII_API_KEY", KEY)
    transport = Transport(route_fn=lambda text: ([], None, None), inline=True)
    transport.start()
    assert transport._uploader is None and transport._remote is None


def test_drain_without_a_thread_is_a_no_op(tmp_path):
    up, opener, _ = make(tmp_path, {"/v1/apps/keys/validate": [(200, VALID)]})
    up.drain(0.1)
    assert opener.calls == []
