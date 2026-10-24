"""The Langfuse export (shared/monitor/SPEC.md "Langfuse export", TS-245/246).

1. The frozen `langfuse` fixture suite (shared/monitor/fixtures/langfuse/),
   run through the generator's own functions, plus the byte check of every
   request body.
2. The exporter against a fake HTTP opener: cursor, batching, scores after
   the span, retries without duplicates, key rejection, fail-open.
3. The `otel` capture in the wrapper (a fake OpenTelemetry API).
No test touches the network or a real Langfuse.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import logging
import types
import uuid
from pathlib import Path

import pytest

from tryaii.monitor import langfuse as lf
from tryaii.monitor import otel, store, wrap
from tryaii.monitor import wrapper_openai as wo

REPO_ROOT = Path(__file__).resolve().parents[3]
CASES = REPO_ROOT / "shared" / "monitor" / "fixtures" / "langfuse" / "cases.json"
GENERATOR = REPO_ROOT / "scripts" / "gen-langfuse-fixtures.py"
APP_KEY = "tryaii_k1a2b3c4d5e6_" + ("fixtureSecret" * 4)[:43]


# --- 1. fixtures -------------------------------------------------------------

def _generator():
    spec = importlib.util.spec_from_file_location("gen_langfuse_fixtures", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cases():
    if not CASES.exists():
        return []
    doc = json.loads(CASES.read_text(encoding="utf-8"))
    return [pytest.param(doc["version"], c, id=c["name"]) for c in doc["cases"]]


fixtures_present = pytest.mark.skipif(not CASES.exists() or not GENERATOR.exists(),
                                      reason="shared fixtures not present (package-only checkout)")


@fixtures_present
@pytest.mark.parametrize("version,case", _cases())
def test_frozen_expected(version, case):
    actual = _generator().gen_case(case["input"], version)
    assert actual == case["expected"]
    # Key order is contract (SPEC.md §1.2): compare the serialized form too.
    assert store.dumps_line(actual) == store.dumps_line(case["expected"])


@fixtures_present
def test_generator_check_passes():
    assert _generator().main(["--check"]) == 0


@fixtures_present
def test_bodies_never_carry_prompt_data():
    doc = json.loads(CASES.read_text(encoding="utf-8"))
    for case in doc["cases"]:
        if case["input"]["kind"] != "export":
            continue
        text = json.dumps(case["expected"]["traces"]) + json.dumps(case["expected"]["scores"])
        for event in case["input"]["events"]:
            assert event["prompt_hash"] not in text
        assert "prompt_hash" not in text and "C:\\\\Users" not in text


# --- 2. exporter -------------------------------------------------------------

class FakeHTTP:
    """urlopen stand-in: records every request and answers from a queue
    (per path; the last answer repeats). An answer is a status or the
    string "network"."""

    def __init__(self, answers=None):
        self.answers = {k: list(v) for k, v in (answers or {}).items()}
        self.requests = []

    def __call__(self, req, timeout=None):
        path = req.full_url.split("://", 1)[1].split("/", 1)[1]
        path = "/" + path
        self.requests.append({"path": path, "auth": req.get_header("Authorization"),
                              "body": json.loads(req.data.decode("utf-8")), "raw": req.data})
        queue = self.answers.get(path) or [200]
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if answer == "network":
            raise OSError("connection refused")
        if answer >= 400:
            import urllib.error

            raise urllib.error.HTTPError(req.full_url, answer, "err", {}, None)
        return _Resp(answer)

    def paths(self):
        return [r["path"] for r in self.requests]


class _Resp:
    def __init__(self, status):
        self.status = status

    def read(self):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _event(n=1, **kw):
    base = {"schema": "tryaii.monitor.event/1", "event_id": str(uuid.UUID(int=n, version=4)),
            "ts": "2026-10-24T09:15:30.250Z", "app": "shop", "process": "checkout",
            "call_site": {"file": "/home/dev/shop/checkout.py", "line": 3, "function": "buy"},
            "provider": "openai", "model_requested": "gpt-5.5", "model_resolved": "openai/gpt-5.5",
            "model_used": "gpt-5.5", "mode": "off", "winner": "openai/gpt-5-mini",
            "winner_reachable": True, "route_status": "ok", "classification": None,
            "taxonomy_version": None,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "cache_ttl_policy": None, "est_cost_usd": 0.0002, "est_cost_winner_usd": 0.00001,
            "est_cost_anchor_usd": 0.0002, "latency_ms": 50, "stream": False, "error": None,
            "prompt_hash": "ab" * 32, "vector": False}
    base.update(kw)
    return base


def _append(directory, *events, raw=None):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "events-2026-10-24.jsonl"
    with open(path, "ab") as fh:
        for e in events:
            fh.write(store.dumps_line(e).encode("utf-8"))
        if raw is not None:
            fh.write(raw)
    return path


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _exporter(http, clock=None):
    return lf.Exporter("http://lf.test/", "pk-lf-1", "sk-lf-1", opener=http,
                       clock=clock or Clock(), now_ts=lambda: "2026-10-24T10:00:00.000Z",
                       version="test")


def test_register_starts_at_the_end_of_the_store(tmp_path):
    d = tmp_path / "shop"
    _append(d, _event(1))
    http = FakeHTTP()
    ex = _exporter(http)
    ex.register(d)
    cursor = lf.read_cursor(d)
    assert cursor["host"] == "http://lf.test"
    size = (d / "events-2026-10-24.jsonl").stat().st_size
    assert cursor["files"] == {"events-2026-10-24.jsonl": size}
    assert ex.run_once() == "idle"
    assert http.requests == []  # no backfill of calls made before the export was on


def test_exports_span_then_scores_and_advances(tmp_path):
    d = tmp_path / "shop"
    http = FakeHTTP()
    ex = _exporter(http)
    ex.register(d)
    _append(d, _event(1), _event(2, winner=None, est_cost_winner_usd=None))
    assert ex.run_once() == "sent"
    assert http.paths() == [lf.TRACES_PATH] + [lf.SCORES_PATH] * 4
    auth = base64.b64encode(b"pk-lf-1:sk-lf-1").decode()
    assert {r["auth"] for r in http.requests} == {f"Basic {auth}"}
    spans = http.requests[0]["body"]["resourceSpans"][0]["scopeSpans"][0]["spans"]
    expected = [lf.derived_ids(_event(n)["event_id"])[1] for n in (1, 2)]
    assert [s["spanId"] for s in spans] == expected
    # Scores go out in parallel: compare as a multiset.
    names = sorted(r["body"]["name"] for r in http.requests[1:])
    assert names == sorted([lf.SCORE_RECOMMENDED, lf.SCORE_SAVINGS, lf.SCORE_ROUTE_STATUS,
                            lf.SCORE_ROUTE_STATUS])
    raw = b"".join(r["raw"] for r in http.requests)
    assert b"prompt_hash" not in raw and b"abab" not in raw and b"/home/dev" not in raw
    cursor = lf.read_cursor(d)
    size = (d / "events-2026-10-24.jsonl").stat().st_size
    assert cursor["files"]["events-2026-10-24.jsonl"] == size
    assert cursor["last_ok"] == "2026-10-24T10:00:00.000Z" and cursor["last_error"] is None
    assert ex.sent_spans == 2 and ex.sent_scores == 4


def test_score_failure_retries_with_the_same_ids(tmp_path):
    d = tmp_path / "shop"
    http = FakeHTTP({lf.SCORES_PATH: [200, 503, 200]})
    clock = Clock()
    ex = _exporter(http, clock)
    ex.register(d)
    _append(d, _event(1))
    assert ex.run_once() == "backoff"
    cursor = lf.read_cursor(d)
    assert cursor["files"].get("events-2026-10-24.jsonl", 0) == 0
    assert cursor["last_error"] == "scores: http 503"
    assert ex.run_once() == "wait"  # backing off
    clock.t += lf.BACKOFF_MIN_S
    assert ex.run_once() == "sent"
    first, second = http.requests[:4], http.requests[4:]
    assert [r["path"] for r in second] == [lf.TRACES_PATH] + [lf.SCORES_PATH] * 3
    # The retried batch repeats the same span and score ids: Langfuse upserts, no duplicates.
    assert second[0]["raw"] == first[0]["raw"]
    score_ids = sorted(s["id"] for s in lf.build_scores(_event(1)))
    assert sorted(r["body"]["id"] for r in second[1:]) == score_ids
    assert lf.read_cursor(d)["last_error"] is None


def test_network_failure_backs_off_and_never_raises(tmp_path):
    d = tmp_path / "shop"
    ex = _exporter(FakeHTTP({lf.TRACES_PATH: ["network"]}))
    ex.register(d)
    _append(d, _event(1))
    assert ex.run_once() == "backoff"
    assert ex.backoff == lf.BACKOFF_MIN_S
    assert lf.read_cursor(d)["last_error"] == "traces: network"


def test_rejected_keys_turn_the_export_off(tmp_path, caplog):
    d = tmp_path / "shop"
    http = FakeHTTP({lf.TRACES_PATH: [401]})
    ex = _exporter(http)
    ex.register(d)
    _append(d, _event(1))
    with caplog.at_level(logging.WARNING, logger="tryaii.monitor"):
        assert ex.run_once() == "disabled"
    assert ex.run_once() == "disabled"
    assert len(http.requests) == 1
    assert "Langfuse rejected the keys" in caplog.text
    assert lf.read_cursor(d)["last_error"].startswith("traces: http 401")


def test_bad_batch_is_skipped(tmp_path):
    d = tmp_path / "shop"
    http = FakeHTTP({lf.TRACES_PATH: [400]})
    ex = _exporter(http)
    ex.register(d)
    _append(d, _event(1))
    assert ex.run_once() == "sent"  # passed over: retrying cannot fix a 400
    cursor = lf.read_cursor(d)
    assert cursor["files"]["events-2026-10-24.jsonl"] > 0
    assert cursor["last_error"] == "traces: http 400"
    assert http.paths() == [lf.TRACES_PATH]  # no scores for spans Langfuse refused


def test_partial_and_unexportable_lines(tmp_path):
    d = tmp_path / "shop"
    http = FakeHTTP()
    ex = _exporter(http)
    ex.register(d)
    bad = dict(_event(2), messages=[{"role": "user"}])
    path = _append(d, _event(1), bad, raw=b'{"schema":"tryaii.monitor.event/1"')
    assert ex.run_once() == "sent"
    spans = http.requests[0]["body"]["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(spans) == 1
    size = path.stat().st_size
    partial = len(b'{"schema":"tryaii.monitor.event/1"')
    assert lf.read_cursor(d)["files"][path.name] == size - partial


def test_batches_are_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(lf, "MAX_EVENTS", 2)
    d = tmp_path / "shop"
    http = FakeHTTP()
    clock = Clock()
    ex = _exporter(http, clock)
    ex.register(d)
    _append(d, *[_event(n) for n in range(1, 4)])
    assert ex.run_once() == "sent"
    clock.t += lf.BATCH_INTERVAL_S
    assert ex.run_once() == "sent"
    traces = [r for r in http.requests if r["path"] == lf.TRACES_PATH]
    assert [len(t["body"]["resourceSpans"][0]["scopeSpans"][0]["spans"]) for t in traces] == [2, 1]


def test_tryaii_url_uses_the_remote_app_id(tmp_path):
    d = tmp_path / "shop"
    http = FakeHTTP()
    ex = _exporter(http)
    ex.register(d)
    store.write_remote(d, {"schema": "tryaii.monitor.remote/1", "etag": "e",
                           "app_id": "app_abc123def456", "default_mode": None,
                           "processes": {}}, "2026-10-24T09:00:00.000Z")
    _append(d, _event(1))
    ex.run_once()
    attrs = http.requests[0]["body"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
    url = next(a["value"]["stringValue"] for a in attrs
               if a["key"] == "langfuse.observation.metadata.tryaii_url")
    assert url == "https://tryaii.com/dashboard/monitor/app_abc123def456/features/checkout"


def test_from_env_off_warns_once(caplog):
    lf._warned.clear()
    with caplog.at_level(logging.WARNING, logger="tryaii.monitor"):
        assert lf.Exporter.from_env({"TRYAII_LANGFUSE": "on"}) is None
        assert lf.Exporter.from_env({"TRYAII_LANGFUSE": "on"}) is None
        assert lf.Exporter.from_env({}) is None
    assert caplog.text.count("the Langfuse export is off (TRYAII_API_KEY is not set)") == 1
    ex = lf.Exporter.from_env({"TRYAII_LANGFUSE": "on", "TRYAII_API_KEY": APP_KEY,
                               "LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk",
                               "LANGFUSE_HOST": "http://localhost:3100/"})
    assert ex is not None and ex.host == "http://localhost:3100"


def test_host_change_restarts_the_cursor(tmp_path):
    d = tmp_path / "shop"
    _append(d, _event(1))
    lf.write_cursor(d, {"host": "https://cloud.langfuse.com",
                        "files": {"events-2026-10-24.jsonl": 0}})
    ex = _exporter(FakeHTTP())
    ex.register(d)
    assert lf.read_cursor(d)["host"] == "http://lf.test"
    assert ex.run_once() == "idle"


def test_status_line_in_the_cli(tmp_path, monkeypatch, capsys):
    from tryaii.cli.monitor import _verb_status

    monkeypatch.setenv("TRYAII_DRE_DATA_DIR", str(tmp_path))
    _append(tmp_path / "monitor" / "shop", _event(1))
    for name, value in (("TRYAII_LANGFUSE", "on"), ("TRYAII_API_KEY", APP_KEY),
                        ("LANGFUSE_PUBLIC_KEY", "pk"), ("LANGFUSE_SECRET_KEY", "sk"),
                        ("LANGFUSE_BASE_URL", "http://localhost:3100")):
        monkeypatch.setenv(name, value)
    _verb_status(["--now", "2026-10-24T10:00:00Z"], {})
    assert capsys.readouterr().out.splitlines()[-1] == "langfuse: on (http://localhost:3100)"
    monkeypatch.delenv("TRYAII_LANGFUSE")
    _verb_status(["--now", "2026-10-24T10:00:00Z"], {})
    assert not capsys.readouterr().out.splitlines()[-1].startswith("langfuse:")


# --- 3. otel capture -----------------------------------------------------------

class _SpanContext:
    def __init__(self, trace_id, span_id):
        self.trace_id, self.span_id = trace_id, span_id
        self.is_valid = bool(trace_id and span_id)


class _Span:
    def __init__(self, ctx):
        self._ctx = ctx

    def get_span_context(self):
        return self._ctx


def _fake_trace(trace_id, span_id):
    return types.SimpleNamespace(get_current_span=lambda: _Span(_SpanContext(trace_id, span_id)))


def test_active_span_ids(monkeypatch):
    monkeypatch.setattr(otel, "_trace",
                        _fake_trace(0x4BF92F3577B34DA6A3CE929D0E0E4736, 0x00F067AA0BA902B7))
    assert otel.active_span_ids() == {"trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
                                      "span_id": "00f067aa0ba902b7"}
    monkeypatch.setattr(otel, "_trace", _fake_trace(0, 0))
    assert otel.active_span_ids() is None
    monkeypatch.setattr(otel, "_trace", None)  # opentelemetry not installed
    assert otel.active_span_ids() is None
    broken = types.SimpleNamespace(get_current_span=lambda: 1 / 0)
    monkeypatch.setattr(otel, "_trace", broken)
    assert otel.active_span_ids() is None


class _Completions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(model=kwargs.get("model"),
                                     usage={"prompt_tokens": 3, "completion_tokens": 2,
                                            "total_tokens": 5})


class _Client:
    def __init__(self):
        self.base_url = "https://api.openai.com/v1"
        self.chat = types.SimpleNamespace(completions=_Completions())


@pytest.mark.parametrize("active", [True, False])
def test_wrapper_records_otel(monkeypatch, tmp_path, active):
    monkeypatch.setenv("TRYAII_DRE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TRYAII_MONITOR_MODE", raising=False)
    monkeypatch.delenv("TRYAII_APP", raising=False)
    trace = _fake_trace(0xABC, 0xDEF) if active else _fake_trace(0, 0)
    monkeypatch.setattr(otel, "_trace", trace)
    client = _Client()
    monitor = wrap(client, app="shop", process="p",
                   route_fn=lambda text: (
                       [{"modelId": "openai/gpt-5-mini", "qualityScore": 0.8}], None, None))
    client.chat.completions.create(model="gpt-5.5", messages=[{"role": "user", "content": "hi"}])
    event = store.read_recent_events(monitor.dir, 5)[-1]
    if active:
        assert event["otel"] == {"trace_id": f"{0xABC:032x}", "span_id": f"{0xDEF:016x}"}
        keys = list(event)
        assert keys.index("otel") == keys.index("vector") + 1
    else:
        assert "otel" not in event
    assert isinstance(wo.CallPlan.__dataclass_fields__["otel"].default, type(None))


def test_ids_are_sha256_of_the_event_id():
    digest = hashlib.sha256(b"00000000-0000-4000-8000-000000000001").hexdigest()
    assert lf.derived_ids("00000000-0000-4000-8000-000000000001") == (digest[:32], digest[32:48])


# --- 4. the TryAii parent for Langfuse's OpenAI integration (Python) ---------

def _lf_wrapper(wrapped, instance, args, kwargs):  # stands in for langfuse.openai's
    return wrapped(*args, **kwargs)


_lf_wrapper.__module__ = "langfuse.openai"


class _Bound:
    """What wrapt's BoundFunctionWrapper looks like to the wrapper."""

    def __init__(self, fn, wrapper):
        self._fn, self._self_wrapper = fn, wrapper
        self.__wrapped__ = fn

    def __call__(self, *args, **kwargs):
        return self._fn(*args, **kwargs)


class _Patched:
    """A class attribute patched like `import langfuse.openai` does."""

    def __init__(self, fn, wrapper=_lf_wrapper):
        self._fn, self._wrapper = fn, wrapper

    def __get__(self, instance, owner):
        return _Bound(self._fn.__get__(instance, owner), self._wrapper)


def test_langfuse_wrapped_detection():
    assert otel.langfuse_wrapped(_Bound(lambda: 1, _lf_wrapper))
    other = lambda *a: None  # noqa: E731
    assert not otel.langfuse_wrapped(_Bound(lambda: 1, other))
    assert not otel.langfuse_wrapped(lambda: 1)
    assert not otel.langfuse_wrapped(None)


def _lf_client(seen):
    class Completions:
        def create(self, **kwargs):
            seen.append(otel.active_span_ids())  # what Langfuse's generation would parent to
            return types.SimpleNamespace(model=kwargs.get("model"),
                                         usage={"prompt_tokens": 3, "completion_tokens": 2,
                                                "total_tokens": 5})

    Completions.create = _Patched(Completions.__dict__["create"])
    return types.SimpleNamespace(base_url="https://api.openai.com/v1",
                                 chat=types.SimpleNamespace(completions=Completions()))


def _lf_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TRYAII_DRE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TRYAII_MONITOR_MODE", raising=False)
    monkeypatch.delenv("TRYAII_APP", raising=False)
    for name, value in (("TRYAII_LANGFUSE", "on"), ("TRYAII_API_KEY", APP_KEY),
                        ("LANGFUSE_PUBLIC_KEY", "pk"), ("LANGFUSE_SECRET_KEY", "sk")):
        monkeypatch.setenv(name, value)


class _FakeContext:
    """attach/detach of a fake OpenTelemetry: the attached ids become active."""

    def __init__(self, monkeypatch):
        self.active = None
        self.attached = []
        monkeypatch.setattr(otel, "_trace", types.SimpleNamespace(get_current_span=self._current))
        monkeypatch.setattr(otel, "attach_parent", self._attach)
        monkeypatch.setattr(otel, "detach", self._detach)

    def _current(self):
        ids = self.active
        if ids is None:
            return _Span(_SpanContext(0, 0))
        return _Span(_SpanContext(int(ids[0], 16), int(ids[1], 16)))

    def _attach(self, trace_id, span_id):
        self.attached.append((trace_id, span_id))
        previous, self.active = self.active, (trace_id, span_id)
        return ("token", previous)

    def _detach(self, token):
        if token is not None:
            self.active = token[1]


ROUTE = staticmethod(
    lambda text: ([{"modelId": "openai/gpt-5-mini", "qualityScore": 0.8}], None, None))


def test_parent_made_for_langfuse_integration(monkeypatch, tmp_path):
    _lf_env(monkeypatch, tmp_path)
    fake = _FakeContext(monkeypatch)
    seen = []
    client = _lf_client(seen)
    monitor = wrap(client, app="shop", process="p", route_fn=ROUTE)
    client.chat.completions.create(model="gpt-5.5", messages=[{"role": "user", "content": "hi"}])
    event = store.read_recent_events(monitor.dir, 5)[-1]
    trace_id, span_id = lf.derived_ids(event["event_id"])
    assert event["otel"] == {"trace_id": trace_id, "span_id": span_id, "origin": "tryaii"}
    assert seen == [{"trace_id": trace_id, "span_id": span_id}]  # active inside the call
    assert fake.active is None  # detached after the call
    ids = lf.span_ids(event)
    assert ids == {"trace_id": trace_id, "span_id": span_id, "parent_span_id": None}
    assert lf.build_span(event)["attributes"][0]["value"]["stringValue"] == "span"


def test_no_parent_with_a_user_span_or_without_langfuse(monkeypatch, tmp_path):
    _lf_env(monkeypatch, tmp_path)
    fake = _FakeContext(monkeypatch)
    fake.active = (f"{0xABC:032x}", f"{0xDEF:016x}")  # a user span is open
    client = _lf_client([])
    monitor = wrap(client, app="shop", process="p", route_fn=ROUTE)
    client.chat.completions.create(model="gpt-5.5", messages=[{"role": "user", "content": "hi"}])
    assert store.read_recent_events(monitor.dir, 5)[-1]["otel"] == {
        "trace_id": f"{0xABC:032x}", "span_id": f"{0xDEF:016x}"}
    assert fake.attached == []
    fake.active = None
    plain = _Client()  # no Langfuse integration on the class
    m2 = wrap(plain, app="shop2", process="p", route_fn=ROUTE)
    plain.chat.completions.create(model="gpt-5.5", messages=[{"role": "user", "content": "hi"}])
    assert "otel" not in store.read_recent_events(m2.dir, 5)[-1]
    assert fake.attached == []


def test_no_parent_when_export_off(monkeypatch, tmp_path):
    _lf_env(monkeypatch, tmp_path)
    monkeypatch.delenv("TRYAII_LANGFUSE")
    fake = _FakeContext(monkeypatch)
    client = _lf_client([])
    monitor = wrap(client, app="shop", process="p", route_fn=ROUTE)
    client.chat.completions.create(model="gpt-5.5", messages=[{"role": "user", "content": "hi"}])
    assert "otel" not in store.read_recent_events(monitor.dir, 5)[-1]
    assert fake.attached == []


def test_attach_failure_records_no_otel(monkeypatch, tmp_path):
    _lf_env(monkeypatch, tmp_path)
    fake = _FakeContext(monkeypatch)
    monkeypatch.setattr(otel, "attach_parent", lambda t, s: None)
    client = _lf_client([])
    monitor = wrap(client, app="shop", process="p", route_fn=ROUTE)
    client.chat.completions.create(model="gpt-5.5", messages=[{"role": "user", "content": "hi"}])
    assert "otel" not in store.read_recent_events(monitor.dir, 5)[-1]
    assert fake.active is None


def test_attach_parent_with_the_real_api(monkeypatch):
    pytest.importorskip("opentelemetry.trace")
    monkeypatch.setattr(otel, "_trace", otel._UNSET)
    token = otel.attach_parent("4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7")
    try:
        assert otel.active_span_ids() == {"trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
                                          "span_id": "00f067aa0ba902b7"}
    finally:
        otel.detach(token)
    assert otel.active_span_ids() is None
