"""The openai wrapper, transport and vectors (shared/monitor/SPEC.md §2-§3).

Everything runs against a duck-typed fake client and an injected
``route_fn`` (no openai import, no daemon, no embedding model). The clock is
pinned through ``wrapper_openai._utcnow``.
"""

from __future__ import annotations

import asyncio
import functools
import json
import sys
import threading
import uuid
from datetime import datetime, timezone

import pytest

from tryaii.config import TryaiiDreConfig
from tryaii.monitor import process, store, wrap
from tryaii.monitor import wrapper_openai as wo
from tryaii.monitor.transport import Transport

NOW = datetime(2026, 10, 11, 12, 0, 0, 123000, tzinfo=timezone.utc)
MESSAGES = [{"role": "system", "content": "You are terse."},
            {"role": "user", "content": "Fix the failing test in checkout.py"}]
CLASSIFICATION = {"broadCategory": "coding", "subcategory": "debugging", "confidence": 0.8123}
EMBEDDING = {"model": "fake-embed", "dim": 3, "v": [0.1234, -0.5, 1]}

# Starter-catalog prices (USD / 1K, blended): gpt-5.5 0.0175, gpt-5-mini 0.001125,
# claude-haiku-4.5 0.003. quality floor = anchor - 0.05.
SCORES_MINI_WINS = [
    {"modelId": "openai/gpt-5-mini", "qualityScore": 0.80},
    {"modelId": "openai/gpt-5.5", "qualityScore": 0.82},
    {"modelId": "anthropic/claude-haiku-4.5", "qualityScore": 0.79},
]
SCORES_HAIKU_WINS = [
    {"modelId": "anthropic/claude-haiku-4.5", "qualityScore": 0.81},
    {"modelId": "openai/gpt-5.5", "qualityScore": 0.82},
    {"modelId": "openai/gpt-5-mini", "qualityScore": 0.70},
]


# --- fakes ------------------------------------------------------------------

class FakeUsage:
    def __init__(self, prompt_tokens=100, completion_tokens=50):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = prompt_tokens + completion_tokens

    def model_dump(self):
        return {"prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens}


class FakeResponse:
    def __init__(self, model, usage=None):
        self.model = model
        self.usage = usage


class FakeChunk:
    def __init__(self, text, usage=None):
        self.text = text
        self.usage = usage


class FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    def __iter__(self):
        return iter(self._chunks)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        self.closed = True


class FakeAsyncStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        async def gen():
            for c in self._chunks:
                yield c
        return gen()

    async def aclose(self):
        pass


class RateLimitError(Exception):
    status_code = 429


class FakeCompletions:
    def __init__(self, *, raise_exc=None, usage=True):
        self.calls = []
        self.raise_exc = raise_exc
        self.usage = usage

    def create(self, **kwargs):
        self.calls.append(dict(kwargs))
        if self.raise_exc is not None:
            raise self.raise_exc
        if kwargs.get("stream"):
            last = FakeChunk("", FakeUsage() if self.usage else None)
            chunks = [FakeChunk("a"), FakeChunk("b"), last]
            return FakeStream(chunks)
        return FakeResponse(kwargs.get("model"), FakeUsage() if self.usage else None)


class FakeAsyncCompletions(FakeCompletions):
    async def create(self, **kwargs):  # type: ignore[override]
        self.calls.append(dict(kwargs))
        if self.raise_exc is not None:
            raise self.raise_exc
        if kwargs.get("stream"):
            return FakeAsyncStream([FakeChunk("a"), FakeChunk("", FakeUsage())])
        return FakeResponse(kwargs.get("model"), FakeUsage())


def required_args(fn):
    """A sync decorator around an ``async def``, like ``openai._utils.required_args``
    on the real ``AsyncCompletions.create``: not a coroutine function itself."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)
    return wrapper


class FakeDecoratedAsyncCompletions(FakeCompletions):
    @required_args
    async def create(self, **kwargs):  # type: ignore[override]
        self.calls.append(dict(kwargs))
        await asyncio.sleep(0.01)
        if kwargs.get("stream"):
            return FakeAsyncStream([FakeChunk("a"), FakeChunk("", FakeUsage())])
        return FakeResponse(kwargs.get("model"), FakeUsage())


class FakeChat:
    def __init__(self, completions):
        self.completions = completions


class FakeClient:
    def __init__(self, base_url="https://api.openai.com/v1", completions=None):
        self.base_url = base_url
        self.chat = FakeChat(completions or FakeCompletions())


def make_route_fn(scores, embedding=EMBEDDING, calls=None):
    def route_fn(text):
        if calls is not None:
            calls.append(text)
        return scores, CLASSIFICATION, embedding
    return route_fn


def events_of(monitor):
    return store.read_recent_events(monitor.dir, 50)


def vectors_of(monitor):
    path = store.vectors_path(monitor.dir, wo.format_ts(NOW))
    return store.read_lines(path)


@pytest.fixture(autouse=True)
def _pinned(monkeypatch, tmp_path):
    monkeypatch.setenv("TRYAII_DRE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TRYAII_MONITOR_MODE", raising=False)
    monkeypatch.delenv("TRYAII_APP", raising=False)
    monkeypatch.delenv("TRYAII_MONITOR_VECTORS", raising=False)
    monkeypatch.setattr(wo, "_utcnow", lambda: NOW)
    yield


# --- off: capture + shadow routing -------------------------------------------

def test_off_records_event_and_vector_without_touching_the_call():
    client = FakeClient()
    monitor = wrap(client, app="shop", process="checkout",
                   route_fn=make_route_fn(SCORES_MINI_WINS))
    resp = client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)

    assert resp.model == "gpt-5.5"
    assert client.chat.completions.calls[0]["model"] == "gpt-5.5"
    assert "tryaii_process" not in client.chat.completions.calls[0]
    [event] = events_of(monitor)
    assert list(event) == [
        "schema", "event_id", "ts", "app", "process", "call_site", "provider",
        "model_requested", "model_resolved", "model_used", "mode", "winner",
        "winner_reachable", "route_status", "classification", "taxonomy_version", "usage",
        "cache_ttl_policy",
        "est_cost_usd", "est_cost_winner_usd", "est_cost_anchor_usd", "latency_ms",
        "stream", "error",
        "prompt_hash", "vector"]
    assert event["cache_ttl_policy"] is None   # TS-229 fills it from the request
    assert str(uuid.UUID(event["event_id"])) == event["event_id"]
    assert event["taxonomy_version"] is None   # no bundle carries one yet
    assert event["ts"] == "2026-10-11T12:00:00.123Z"
    assert event["app"] == "shop" and event["process"] == "checkout"
    assert event["call_site"]["file"].endswith("test_monitor_wrap.py")
    assert event["call_site"]["function"] == (
        "test_off_records_event_and_vector_without_touching_the_call")
    assert event["provider"] == "openai"
    assert event["model_requested"] == "gpt-5.5"
    assert event["model_resolved"] == "openai/gpt-5.5"
    assert event["model_used"] == "gpt-5.5"
    assert event["mode"] == "off"
    assert event["winner"] == "openai/gpt-5-mini" and event["winner_reachable"] is True
    assert event["route_status"] == "ok"
    assert event["classification"] == {"category": "coding", "subcategory": "debugging",
                                       "confidence": 0.8123}
    assert event["usage"] == {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
                              "cached_tokens": None, "cache_write_tokens": None,
                              "cache_writes": None}
    assert event["est_cost_usd"] == 0.002   # 100*0.005/1k + 50*0.03/1k
    assert event["est_cost_winner_usd"] == 0.000125
    assert event["est_cost_anchor_usd"] == 0.002   # no swap: equals est_cost_usd
    assert event["stream"] is False and event["error"] is None
    assert event["vector"] is True
    [vector] = vectors_of(monitor)
    assert vector["prompt_hash"] == event["prompt_hash"]
    assert vector["model"] == "fake-embed" and vector["dim"] == 3
    assert vector["v"] == [0.1234, -0.5, 1]
    assert vector["process"] == "checkout"


def test_off_never_waits_for_routing_on_the_hot_path():
    gate = threading.Event()
    routed = threading.Event()

    def slow_route(text):
        gate.wait(5)
        routed.set()
        return SCORES_MINI_WINS, CLASSIFICATION, None

    transport = Transport(TryaiiDreConfig(), route_fn=slow_route, inline=False)
    transport._thread = threading.Thread(target=transport._run, daemon=True)
    transport._thread.start()
    client = FakeClient()
    monitor = wo.Monitor("openai", app="shop", process="p", transport=transport)
    original = client.chat.completions.create
    client.chat.completions.create = wo._make_create(monitor, original, False)

    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert not routed.is_set()          # the call returned before routing ran
    assert events_of(monitor) == []
    gate.set()
    transport.drain(5)
    assert routed.is_set()
    [event] = events_of(monitor)
    assert event["winner"] == "openai/gpt-5-mini" and event["vector"] is False


def test_vectors_can_be_disabled(monkeypatch):
    monkeypatch.setenv("TRYAII_MONITOR_VECTORS", "0")
    client = FakeClient()
    monitor = wrap(client, app="shop", route_fn=make_route_fn(SCORES_MINI_WINS))
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    [event] = events_of(monitor)
    assert event["vector"] is False
    assert vectors_of(monitor) == []


def test_same_prompt_twice_writes_one_vector():
    client = FakeClient()
    monitor = wrap(client, app="shop", route_fn=make_route_fn(SCORES_MINI_WINS))
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert [e["vector"] for e in events_of(monitor)] == [True, False]
    assert len(vectors_of(monitor)) == 1


def test_no_user_message_is_recorded_but_not_routed():
    calls = []
    client = FakeClient()
    monitor = wrap(client, app="shop", route_fn=make_route_fn(SCORES_MINI_WINS, calls=calls))
    client.chat.completions.create(model="gpt-5.5", messages=[{"role": "system", "content": "s"}])
    [event] = events_of(monitor)
    assert event["route_status"] == "skipped" and event["winner"] is None
    assert calls == []


def test_route_failures_are_recorded_as_status():
    def boom(text):
        raise RuntimeError("daemon gone")

    def slow(text):
        raise TimeoutError()

    for fn, status in ((boom, "unavailable"), (slow, "timeout")):
        client = FakeClient()
        monitor = wrap(client, app=f"app-{status}", route_fn=fn)
        resp = client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
        assert resp.model == "gpt-5.5"
        [event] = events_of(monitor)
        assert event["route_status"] == status
        assert event["winner"] is None and event["est_cost_usd"] == 0.002


# --- dynamic ----------------------------------------------------------------

def test_dynamic_sends_the_reachable_winner():
    calls = []
    client = FakeClient()
    monitor = wrap(client, app="shop", mode="dynamic",
                   route_fn=make_route_fn(SCORES_MINI_WINS, calls=calls))
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert client.chat.completions.calls[0]["model"] == "gpt-5-mini"
    # In-path over the reachable (openai/*) candidates, then the shadow route
    # over the whole (policy-allowed) catalog for the recommendation.
    assert len(calls) == 2
    [event] = events_of(monitor)
    assert event["mode"] == "dynamic"
    assert event["model_requested"] == "gpt-5.5" and event["model_used"] == "gpt-5-mini"
    assert event["est_cost_usd"] == 0.000125 and event["est_cost_winner_usd"] == 0.000125
    # After a swap est_cost_usd is priced at the model sent; the anchor price
    # makes the realised saving computable (API.md v1.1).
    assert event["est_cost_anchor_usd"] == 0.002


def test_dynamic_keeps_the_anchor_when_the_winner_is_unreachable():
    client = FakeClient()
    monitor = wrap(client, app="shop", mode="dynamic", route_fn=make_route_fn(SCORES_HAIKU_WINS))
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert client.chat.completions.calls[0]["model"] == "gpt-5.5"
    [event] = events_of(monitor)
    assert event["winner"] == "anthropic/claude-haiku-4.5"
    assert event["winner_reachable"] is False
    assert event["model_used"] == "gpt-5.5"


def test_openrouter_client_can_reach_every_provider():
    client = FakeClient(base_url="https://openrouter.ai/api/v1")
    monitor = wrap(client, app="shop", mode="dynamic", route_fn=make_route_fn(SCORES_HAIKU_WINS))
    client.chat.completions.create(model="openai/gpt-5.5", messages=MESSAGES)
    assert client.chat.completions.calls[0]["model"] == "anthropic/claude-haiku-4.5"
    [event] = events_of(monitor)
    assert event["provider"] == "openrouter" and event["winner_reachable"] is True


def test_other_provider_records_but_never_swaps():
    client = FakeClient(base_url="http://localhost:11434/v1")
    monitor = wrap(client, app="shop", mode="dynamic", route_fn=make_route_fn(SCORES_MINI_WINS))
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert client.chat.completions.calls[0]["model"] == "gpt-5.5"
    [event] = events_of(monitor)
    assert event["provider"] == "other"
    assert event["winner"] == "openai/gpt-5-mini" and event["winner_reachable"] is False


def test_mode_comes_from_config_json_and_env(monkeypatch):
    client = FakeClient()
    monitor = wrap(client, app="shop", process="checkout",
                   route_fn=make_route_fn(SCORES_MINI_WINS))
    store.write_config(monitor.dir, {
        "schema": "tryaii.monitor.config/1", "source": "local", "default_mode": "off",
        "processes": {"checkout": {"mode": "dynamic", "providers": None}}})
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert client.chat.completions.calls[-1]["model"] == "gpt-5-mini"
    monkeypatch.setenv("TRYAII_MONITOR_MODE", "off")
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert client.chat.completions.calls[-1]["model"] == "gpt-5.5"
    assert [e["mode"] for e in events_of(monitor)] == ["dynamic", "off"]


# --- stable -----------------------------------------------------------------

def test_stable_pins_the_first_winner_and_reuses_it():
    calls = []
    client = FakeClient()
    monitor = wrap(client, app="shop", process="checkout", mode="stable",
                   route_fn=make_route_fn(SCORES_MINI_WINS, calls=calls))
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert client.chat.completions.calls[0]["model"] == "gpt-5-mini"
    pins = json.loads((monitor.dir / "pins.json").read_text(encoding="utf-8"))
    pin = pins["processes"]["checkout"]
    assert pin["model"] == "openai/gpt-5-mini"
    assert pin["pinned_at"] == "2026-10-11T12:00:00.123Z"
    assert pin["expires_at"] == "2026-10-12T12:00:00.123Z"
    assert pin["basis"] == {"window": "first_call", "events": 1, "counts": {"openai/gpt-5-mini": 1}}
    assert len(calls) == 2               # in-path (reachable) + shadow (recommendation)

    # Second call: the pin is valid, so no in-path routing; shadow routing still runs.
    calls.clear()
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert client.chat.completions.calls[1]["model"] == "gpt-5-mini"
    assert len(calls) == 1
    pins2 = json.loads((monitor.dir / "pins.json").read_text(encoding="utf-8"))
    assert pins2 == pins
    assert [e["mode"] for e in events_of(monitor)] == ["stable", "stable"]


def test_stable_first_call_pins_the_best_reachable_model_not_the_unreachable_winner():
    client = FakeClient()
    monitor = wrap(client, app="shop", process="checkout", mode="stable",
                   route_fn=make_route_fn(SCORES_HAIKU_WINS))
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    # Over the reachable candidates nothing beats the anchor at its price:
    # the anchor is pinned (and sent as written); haiku stays the
    # recommendation, recorded as unreachable.
    assert client.chat.completions.calls[0]["model"] == "gpt-5.5"
    pins = json.loads((monitor.dir / "pins.json").read_text(encoding="utf-8"))
    assert pins["processes"]["checkout"]["model"] == "openai/gpt-5.5"
    [event] = events_of(monitor)
    assert event["winner"] == "anthropic/claude-haiku-4.5"
    assert event["winner_reachable"] is False


def test_stable_rebuilds_an_expired_pin_from_history():
    client = FakeClient()
    monitor = wrap(client, app="shop", process="checkout", mode="stable",
                   route_fn=make_route_fn(SCORES_MINI_WINS))
    # History: a different reachable winner won the recent calls (fewer than
    # 20 in 24 h, so the engine widens to the 7 d window).
    for i in range(3):
        store.append_event(monitor.dir, {
            "schema": "tryaii.monitor.event/1", "ts": f"2026-10-11T0{i}:00:00.000Z",
            "app": "shop", "process": "checkout", "winner": "openai/gpt-5-nano",
            "winner_reachable": True, "route_status": "ok"})
    store.write_pins(monitor.dir, {"schema": "tryaii.monitor.pins/1", "processes": {"checkout": {
        "model": "openai/gpt-5-mini", "pinned_at": "2026-10-09T00:00:00.000Z",
        "expires_at": "2026-10-10T00:00:00.000Z",
        "basis": {"window": "first_call", "events": 1, "counts": {"openai/gpt-5-mini": 1}}}}})
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert client.chat.completions.calls[0]["model"] == "gpt-5-nano"
    doc = json.loads((monitor.dir / "pins.json").read_text(encoding="utf-8"))
    pin = doc["processes"]["checkout"]
    assert pin["model"] == "openai/gpt-5-nano"
    assert pin["basis"]["window"] == "7d" and pin["basis"]["counts"] == {"openai/gpt-5-nano": 3}


# --- errors and streams -----------------------------------------------------

def test_provider_error_is_reraised_unchanged_and_recorded():
    exc = RateLimitError("slow down")
    client = FakeClient(completions=FakeCompletions(raise_exc=exc))
    monitor = wrap(client, app="shop", route_fn=make_route_fn(SCORES_MINI_WINS))
    with pytest.raises(RateLimitError) as info:
        client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert info.value is exc
    [event] = events_of(monitor)
    assert event["error"] == {"status": 429, "class": "RateLimitError"}
    assert event["usage"] is None and event["est_cost_usd"] is None
    # A failed call is never routed and is written at once (survives an exit).
    assert event["winner"] is None and event["route_status"] == "skipped"


def test_stream_is_forwarded_and_recorded_at_the_end():
    client = FakeClient()
    monitor = wrap(client, app="shop", route_fn=make_route_fn(SCORES_MINI_WINS))
    stream = client.chat.completions.create(model="gpt-5.5", messages=MESSAGES, stream=True)
    assert client.chat.completions.calls[0]["stream_options"] == {"include_usage": True}
    assert events_of(monitor) == []
    texts = [c.text for c in stream]
    assert texts == ["a", "b", ""]
    [event] = events_of(monitor)
    assert event["stream"] is True
    assert event["usage"] == {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
                              "cached_tokens": None, "cache_write_tokens": None,
                              "cache_writes": None}


def test_stream_closed_early_is_still_recorded():
    client = FakeClient()
    monitor = wrap(client, app="shop", route_fn=make_route_fn(SCORES_MINI_WINS))
    with client.chat.completions.create(model="gpt-5.5", messages=MESSAGES, stream=True) as stream:
        next(iter(stream))
    assert stream.closed is True
    [event] = events_of(monitor)
    assert event["stream"] is True and event["usage"] is None


def test_existing_stream_options_are_left_alone():
    client = FakeClient(base_url="http://localhost:8000/v1")
    wrap(client, app="shop", route_fn=make_route_fn(SCORES_MINI_WINS))
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES, stream=True)
    assert "stream_options" not in client.chat.completions.calls[0]


async def test_async_client_sync_capture_and_await():
    client = FakeClient(completions=FakeAsyncCompletions())
    monitor = wrap(client, app="shop", mode="dynamic", route_fn=make_route_fn(SCORES_MINI_WINS))
    resp = await client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert resp.model == "gpt-5-mini"
    [event] = events_of(monitor)
    assert event["model_used"] == "gpt-5-mini"
    assert event["call_site"]["function"] == "test_async_client_sync_capture_and_await"

    stream = await client.chat.completions.create(model="gpt-5.5", messages=MESSAGES, stream=True)
    texts = [c.text async for c in stream]
    assert texts == ["a", ""]
    events = events_of(monitor)
    assert events[-1]["stream"] is True and events[-1]["usage"]["total_tokens"] == 150


async def test_async_create_behind_a_sync_decorator_is_detected():
    # The real AsyncCompletions.create is a sync def (required_args) around
    # an async def: it must still be awaited, not recorded as a sync call.
    client = FakeClient(completions=FakeDecoratedAsyncCompletions())
    monitor = wrap(client, app="shop", route_fn=make_route_fn(SCORES_MINI_WINS))
    resp = await client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert resp.model == "gpt-5.5"
    [event] = events_of(monitor)
    assert event["usage"] == {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
                              "cached_tokens": None, "cache_write_tokens": None,
                              "cache_writes": None}
    assert event["latency_ms"] > 0

    stream = await client.chat.completions.create(model="gpt-5.5", messages=MESSAGES, stream=True)
    assert len(events_of(monitor)) == 1
    texts = [c.text async for c in stream]
    assert texts == ["a", ""]
    event = events_of(monitor)[-1]
    assert event["stream"] is True and event["usage"]["total_tokens"] == 150
    assert event["latency_ms"] > 0


def test_is_coroutine_function_survives_a_broken_wrapped_chain():
    def loop():
        pass

    loop.__wrapped__ = loop  # inspect.unwrap raises ValueError on a cycle
    assert wo._is_coroutine_function(loop) is False


# --- wrapper order -------------------------------------------------------------

def _counting(original, counter):
    """A late class-level patch (like ``import langfuse.openai``): a plain
    ``def`` that calls through."""
    def create(self, **kwargs):
        counter.append(kwargs.get("model"))
        return original(self, **kwargs)
    return create


def test_class_patch_after_wrap_is_still_called(monkeypatch):
    counter = []
    client = FakeClient()
    monitor = wrap(client, app="shop", route_fn=make_route_fn(SCORES_MINI_WINS))
    monkeypatch.setattr(FakeCompletions, "create",
                        _counting(FakeCompletions.__dict__["create"], counter))
    resp = client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert resp.model == "gpt-5.5"
    assert counter == ["gpt-5.5"]
    assert len(client.chat.completions.calls) == 1      # sent once
    [event] = events_of(monitor)
    assert event["usage"]["total_tokens"] == 150


def test_rewrap_still_follows_a_later_class_patch(monkeypatch):
    counter = []
    client = FakeClient()
    wrap(client, app="one", route_fn=make_route_fn(SCORES_MINI_WINS))
    monitor = wrap(client, app="two", route_fn=make_route_fn(SCORES_MINI_WINS))
    monkeypatch.setattr(FakeCompletions, "create",
                        _counting(FakeCompletions.__dict__["create"], counter))
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert counter == ["gpt-5.5"]
    assert len(client.chat.completions.calls) == 1
    assert [e["app"] for e in events_of(monitor)] == ["two"]


async def test_async_class_patch_after_wrap_is_still_called(monkeypatch):
    counter = []
    client = FakeClient(completions=FakeDecoratedAsyncCompletions())
    monitor = wrap(client, app="shop", route_fn=make_route_fn(SCORES_MINI_WINS))
    monkeypatch.setattr(FakeDecoratedAsyncCompletions, "create",
                        _counting(FakeDecoratedAsyncCompletions.__dict__["create"], counter))
    resp = await client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert resp.model == "gpt-5.5"
    stream = await client.chat.completions.create(model="gpt-5.5", messages=MESSAGES, stream=True)
    assert [c.text async for c in stream] == ["a", ""]
    assert counter == ["gpt-5.5", "gpt-5.5"]
    assert len(client.chat.completions.calls) == 2
    events = events_of(monitor)
    assert [e["usage"]["total_tokens"] for e in events] == [150, 150]
    assert all(e["latency_ms"] > 0 for e in events)


def test_create_set_on_the_instance_before_wrap_is_kept(monkeypatch):
    seen, counter = [], []
    client = FakeClient()
    inner = client.chat.completions.create

    def instance_create(**kwargs):
        seen.append(kwargs.get("model"))
        return inner(**kwargs)

    client.chat.completions.create = instance_create
    monitor = wrap(client, app="shop", route_fn=make_route_fn(SCORES_MINI_WINS))
    monkeypatch.setattr(FakeCompletions, "create",
                        _counting(FakeCompletions.__dict__["create"], counter))
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert seen == ["gpt-5.5"] and counter == []
    assert len(client.chat.completions.calls) == 1
    assert len(events_of(monitor)) == 1


# --- identity ---------------------------------------------------------------

def test_process_precedence_call_context_args_callsite():
    client = FakeClient()
    monitor = wrap(client, app="shop", process="from-args",
                   route_fn=make_route_fn(SCORES_MINI_WINS))
    with process("from-context"):
        client.chat.completions.create(model="gpt-5.5", messages=MESSAGES,
                                       tryaii_process="per-call")
        client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert [e["process"] for e in events_of(monitor)] == ["per-call", "from-context", "from-args"]

    bare = FakeClient()
    monitor2 = wrap(bare, app="shop2", route_fn=make_route_fn(SCORES_MINI_WINS))
    bare.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    [event] = events_of(monitor2)
    assert event["process"] == (
        "test_monitor_wrap.py:test_process_precedence_call_context_args_callsite")


def test_app_comes_from_env_then_args(monkeypatch):
    client = FakeClient()
    monitor = wrap(client, app="From Args!", route_fn=make_route_fn(SCORES_MINI_WINS))
    assert monitor.app == "from-args"
    monkeypatch.setenv("TRYAII_APP", "Env App")
    monitor2 = wrap(FakeClient(), app="from-args", route_fn=make_route_fn(SCORES_MINI_WINS))
    assert monitor2.app == "env-app"
    assert monitor2.dir.parent.name == "monitor"


def test_unknown_model_is_recorded_with_top_winner():
    client = FakeClient()
    monitor = wrap(client, app="shop", route_fn=make_route_fn(SCORES_MINI_WINS))
    client.chat.completions.create(model="my-finetune", messages=MESSAGES)
    [event] = events_of(monitor)
    assert event["model_resolved"] is None and event["est_cost_usd"] is None
    assert event["winner"] == "openai/gpt-5-mini"


def test_wrap_is_fail_open_when_monitor_breaks(monkeypatch):
    client = FakeClient()
    wrap(client, app="shop", route_fn=make_route_fn(SCORES_MINI_WINS))
    def broken(messages):
        raise RuntimeError("x")

    monkeypatch.setattr(wo, "routing_text", broken)
    resp = client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert resp.model == "gpt-5.5"


def test_rewrap_replaces_the_previous_monitor():
    client = FakeClient()
    wrap(client, app="one", route_fn=make_route_fn(SCORES_MINI_WINS))
    monitor = wrap(client, app="two", route_fn=make_route_fn(SCORES_MINI_WINS))
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert len(client.chat.completions.calls) == 1
    assert [e["app"] for e in events_of(monitor)] == ["two"]


def test_install_returns_false_without_openai(monkeypatch):
    monkeypatch.delitem(sys.modules, "openai", raising=False)
    from tryaii.monitor import install

    assert install(app="shop") is False
    assert wo._defaults["app"] == "shop"
    wo._defaults.update({"app": None, "process": None, "mode": None, "cachelint": False})


def test_lazy_package_attribute():
    import tryaii

    assert tryaii.monitor.wrap is wrap


# --- slice 2: managed mode ----------------------------------------------------

def _set_managed(monitor, process, model):
    store.write_config(monitor.dir, {
        "schema": "tryaii.monitor.config/1", "source": "local", "default_mode": "off",
        "processes": {process: {"mode": "managed", "model": model, "providers": None}}})


def test_managed_sends_its_model_and_still_records_the_winner():
    calls = []
    client = FakeClient()
    monitor = wrap(client, app="shop", process="checkout",
                   route_fn=make_route_fn(SCORES_HAIKU_WINS, calls=calls))
    _set_managed(monitor, "checkout", "openai/gpt-5-nano")
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert client.chat.completions.calls[0]["model"] == "gpt-5-nano"
    assert len(calls) == 1                      # shadow only; no in-path routing
    [event] = events_of(monitor)
    assert event["mode"] == "managed" and event["model_used"] == "gpt-5-nano"
    assert event["route_status"] == "ok"
    assert event["winner"] == "anthropic/claude-haiku-4.5"


def test_managed_unreachable_sends_the_anchor_with_its_status():
    client = FakeClient()
    monitor = wrap(client, app="shop", process="checkout",
                   route_fn=make_route_fn(SCORES_MINI_WINS))
    _set_managed(monitor, "checkout", "anthropic/claude-haiku-4.5")
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert client.chat.completions.calls[0]["model"] == "gpt-5.5"
    [event] = events_of(monitor)
    assert event["mode"] == "managed" and event["model_used"] == "gpt-5.5"
    assert event["route_status"] == "unreachable_managed"
    assert event["winner"] == "openai/gpt-5-mini"   # the recommendation is still recorded


def test_managed_without_user_text_still_sends_the_model():
    client = FakeClient()
    monitor = wrap(client, app="shop", process="checkout",
                   route_fn=make_route_fn(SCORES_MINI_WINS))
    _set_managed(monitor, "checkout", "openai/gpt-5-mini")
    client.chat.completions.create(model="gpt-5.5", messages=[{"role": "system", "content": "s"}])
    assert client.chat.completions.calls[0]["model"] == "gpt-5-mini"
    [event] = events_of(monitor)
    assert event["route_status"] == "skipped"


async def test_managed_on_an_async_client():
    client = FakeClient(completions=FakeAsyncCompletions())
    monitor = wrap(client, app="shop", process="checkout",
                   route_fn=make_route_fn(SCORES_MINI_WINS))
    _set_managed(monitor, "checkout", "gpt-5-nano")
    resp = await client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert resp.model == "gpt-5-nano"


# --- slice 2: policy-aware reachability (TS-170) --------------------------------

def _policy_monitor(client, policy, scores, mode="dynamic", calls=None):
    transport = Transport(TryaiiDreConfig(), route_fn=make_route_fn(scores, calls=calls),
                          inline=True, policy=policy)
    transport.start()
    monitor = wo.Monitor(wo._client_provider(client), app="shop", process="p", mode=mode,
                         transport=transport)
    client.chat.completions.create = wo._make_create(
        monitor, client.chat.completions.create, False)
    return monitor


def test_dynamic_sends_the_best_reachable_when_the_winner_is_unreachable():
    scores = [{"modelId": "anthropic/claude-haiku-4.5", "qualityScore": 0.81},
              {"modelId": "openai/gpt-5-mini", "qualityScore": 0.79},
              {"modelId": "openai/gpt-5.5", "qualityScore": 0.82}]
    client = FakeClient()
    monitor = wrap(client, app="shop", mode="dynamic", route_fn=make_route_fn(scores))
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert client.chat.completions.calls[0]["model"] == "gpt-5-mini"
    [event] = events_of(monitor)
    assert event["winner"] == "anthropic/claude-haiku-4.5"
    assert event["winner_reachable"] is False


def test_policy_limits_what_is_sent_and_what_is_recommended():
    client = FakeClient(base_url="https://openrouter.ai/api/v1")
    monitor = _policy_monitor(client, ["openai/gpt-5.5", "openai/gpt-5-mini"],
                              SCORES_HAIKU_WINS)
    client.chat.completions.create(model="openai/gpt-5.5", messages=MESSAGES)
    # haiku tops the ranking but the policy disallows it: never sent, never
    # recommended; gpt-5-mini is below the quality floor -> the anchor.
    assert client.chat.completions.calls[0]["model"] == "openai/gpt-5.5"
    [event] = events_of(monitor)
    assert event["winner"] == "openai/gpt-5.5"


def test_policy_that_allows_nothing_sends_the_anchor_and_recommends_nothing():
    client = FakeClient()
    monitor = _policy_monitor(client, [], SCORES_MINI_WINS)
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert client.chat.completions.calls[0]["model"] == "gpt-5.5"
    [event] = events_of(monitor)
    assert event["winner"] is None and event["route_status"] == "ok"


def test_openrouter_without_policy_routes_once():
    calls = []
    client = FakeClient(base_url="https://openrouter.ai/api/v1")
    _policy_monitor(client, None, SCORES_HAIKU_WINS, calls=calls)
    client.chat.completions.create(model="openai/gpt-5.5", messages=MESSAGES)
    assert len(calls) == 1          # in-path list == shadow list: the outcome is reused


def test_other_provider_skips_in_path_routing():
    calls = []
    client = FakeClient(base_url="http://localhost:11434/v1")
    _policy_monitor(client, None, SCORES_MINI_WINS, calls=calls)
    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    assert len(calls) == 1          # only the shadow route: nothing but the anchor is sendable


def test_saved_policy_is_resolved_once_and_never_touches_the_daemon(monkeypatch, tmp_path):
    from tryaii import daemon as daemon_mod
    from tryaii import policy as pol

    loaded = []
    real = pol.resolve_policy

    def counting(spec="auto"):
        loaded.append(spec)
        return real(spec)

    def no_daemon(*a, **k):
        raise AssertionError("policy evaluation must not touch the daemon")

    monkeypatch.setattr(pol, "resolve_policy", counting)
    monkeypatch.setattr(daemon_mod, "ensure_daemon", no_daemon)
    monkeypatch.setattr(daemon_mod, "_live_state", no_daemon)
    pol.save_policy(pol.ModelPolicy(origin="no-prc-models"))
    had_st = "sentence_transformers" in sys.modules
    transport = Transport(TryaiiDreConfig(), route_fn=make_route_fn(SCORES_MINI_WINS),
                          inline=True)
    # The starter bundle has no origins.json: an active policy fails closed.
    assert transport.policy_allowed() == []
    assert transport.policy_allowed() == []
    assert loaded == ["auto"]
    assert ("sentence_transformers" in sys.modules) == had_st


def test_broken_saved_policy_fails_closed(tmp_path):
    (tmp_path / "policy.json").write_text("{broken", encoding="utf-8")
    transport = Transport(TryaiiDreConfig(), route_fn=make_route_fn(SCORES_MINI_WINS),
                          inline=True)
    assert transport.policy_allowed() == []


def test_no_saved_policy_is_unconstrained():
    transport = Transport(TryaiiDreConfig(), route_fn=make_route_fn(SCORES_MINI_WINS),
                          inline=True)
    assert transport.policy_allowed() is None


def test_daemon_gets_allowed_models_and_the_ranking_is_filtered_anyway(monkeypatch):
    from types import SimpleNamespace

    from tryaii import daemon as daemon_mod

    payloads = []

    def fake_request(state, payload, timeout):
        payloads.append(payload)
        return {"ok": True, "result": {}}

    ranking = [SimpleNamespace(model_id="anthropic/claude-haiku-4.5", quality_score=0.9),
               SimpleNamespace(model_id="openai/gpt-5-mini", quality_score=0.8)]
    monkeypatch.setattr(daemon_mod, "is_disabled", lambda: False)
    monkeypatch.setattr(daemon_mod, "_request", fake_request)
    monkeypatch.setattr(daemon_mod, "_deserialize_route_result",
                        lambda data: SimpleNamespace(scores=ranking, classification=None))
    transport = Transport(TryaiiDreConfig())
    transport._state = {"features": ["allowedModels", "routeEmbedding"]}
    out = transport.route("hi", include_embedding=False, allowed_models=["openai/gpt-5-mini"])
    assert payloads[-1]["allowedModels"] == ["openai/gpt-5-mini"]
    assert [s.model_id for s in out.scores] == ["openai/gpt-5-mini"]
    # An older daemon without the feature: no field, client-side filter only.
    transport._state = {"features": []}
    out = transport.route("hi", include_embedding=False, allowed_models=["openai/gpt-5-mini"])
    assert "allowedModels" not in payloads[-1]
    assert [s.model_id for s in out.scores] == ["openai/gpt-5-mini"]
    out = transport.route("hi", include_embedding=False)
    assert "allowedModels" not in payloads[-1] and len(out.scores) == 2


def test_register_app_reaches_the_services(tmp_path):
    transport = Transport(TryaiiDreConfig(), route_fn=make_route_fn(SCORES_MINI_WINS),
                          inline=True)
    seen = []

    class Svc:
        def register(self, d):
            seen.append(d)

    transport._uploader = Svc()
    client = FakeClient()
    monitor = wo.Monitor("openai", app="shop", transport=transport)
    assert seen == [monitor.dir]
    del client


def test_exit_records_calls_still_waiting_for_the_warm_up(monkeypatch):
    """TS-224: a process that exits before the daemon warm-up finishes still
    records its calls (``route_status: unavailable``) instead of losing them."""
    from tryaii import daemon as daemon_mod
    from tryaii.catalog import client as catalog_client

    gate = threading.Event()

    def offline(*a, **k):
        raise RuntimeError("offline")

    monkeypatch.setattr(daemon_mod, "is_disabled", lambda: False)
    monkeypatch.setattr(daemon_mod, "ensure_daemon", lambda *a, **k: gate.wait(5))
    monkeypatch.setattr(catalog_client, "select_catalog", offline)
    monkeypatch.setattr(Transport, "_embedding_model_ready", lambda self: True)
    transport = Transport(TryaiiDreConfig())
    transport.start()                     # worker + the warm-up job, stuck on the gate
    client = FakeClient()
    monitor = wo.Monitor("openai", app="shop", process="p", transport=transport)
    original = client.chat.completions.create
    client.chat.completions.create = wo._make_create(monitor, original, False)

    client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    transport.drain(0.2)
    assert events_of(monitor) == []       # queued behind the warm-up
    transport._at_exit()                  # what atexit runs
    [event] = events_of(monitor)
    assert event["route_status"] == "unavailable" and event["winner"] is None
    assert event["model_used"] == "gpt-5.5" and event["vector"] is False
    assert event["usage"]["total_tokens"] == 150
    gate.set()


def _no_model_package(monkeypatch):
    from tryaii.embeddings import model_store

    monkeypatch.delenv(model_store.ENV_MODEL_PATH, raising=False)
    monkeypatch.setitem(sys.modules,
                        model_store.MODELS[model_store.DEFAULT_ONNX_MODEL].package, None)
    model_store.reset_cache()


def _offline_daemon(monkeypatch, started):
    from tryaii import daemon as daemon_mod
    from tryaii.catalog import client as catalog_client

    monkeypatch.setattr(daemon_mod, "is_disabled", lambda: False)
    monkeypatch.setattr(daemon_mod, "ensure_daemon",
                        lambda *a, **k: started.append(1) or None)
    monkeypatch.setattr(catalog_client, "select_catalog",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))


@pytest.mark.parametrize("mode", ["off", "dynamic"])
def test_missing_embedding_model_fails_open_without_a_daemon(monkeypatch, tmp_path, caplog,
                                                             mode):
    """TS-252: no model file (package missing, nothing placed) behaves
    exactly like no daemon: no daemon is started, every call is recorded
    metadata-only, the model in the code is sent, and one line is logged."""
    import logging

    started = []
    _no_model_package(monkeypatch)
    _offline_daemon(monkeypatch, started)
    caplog.set_level(logging.DEBUG, logger="tryaii.monitor")

    transport = Transport(TryaiiDreConfig(data_dir=tmp_path))
    transport.start()
    client = FakeClient()
    monitor = wo.Monitor("openai", app="shop", process="p", mode=mode, transport=transport)
    original = client.chat.completions.create
    client.chat.completions.create = wo._make_create(monitor, original, False)
    transport.drain(5)                    # the warm-up has run
    for _ in range(2):
        client.chat.completions.create(model="gpt-5.5", messages=MESSAGES)
    transport.drain(5)

    assert started == []
    assert [c["model"] for c in client.chat.completions.calls] == ["gpt-5.5", "gpt-5.5"]
    events = events_of(monitor)
    assert len(events) == 2
    for event in events:
        assert event["route_status"] == "unavailable" and event["winner"] is None
        assert event["classification"] is None and event["vector"] is False
        assert event["model_used"] == "gpt-5.5" and event["mode"] == mode
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    assert "all-MiniLM-L6-v2-onnx-int8 is not available" in text
    assert "package tryaii-minilm-int8 is not installed" in text
    assert "pip install --force-reinstall tryaii-minilm-int8" in text


def test_missing_model_warning_is_not_repeated_by_every_process(monkeypatch, tmp_path, caplog):
    """A fleet restarting all day logs the warning once a day per machine;
    `tryaii monitor status` shows the state; a fixed install clears it."""
    import logging

    from tryaii.cli.monitor import _classification_line
    from tryaii.embeddings import model_store

    started = []
    _no_model_package(monkeypatch)
    _offline_daemon(monkeypatch, started)
    caplog.set_level(logging.DEBUG, logger="tryaii.monitor")
    for _ in range(3):                    # three process starts
        model_store.reset_cache()
        Transport(TryaiiDreConfig(data_dir=tmp_path))._warm_up()
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1
    line = _classification_line(tmp_path)
    assert line.startswith("classification: off since ")
    assert line.endswith("(package tryaii-minilm-int8 is not installed)")

    monkeypatch.setattr(model_store, "resolve_model_path",
                        lambda *a, **k: tmp_path / "model.onnx")
    Transport(TryaiiDreConfig(data_dir=tmp_path))._warm_up()
    assert _classification_line(tmp_path) is None
    assert started == [1]


def test_embedding_model_present_lets_the_daemon_start(monkeypatch, tmp_path):
    from tryaii.embeddings import model_store

    started = []
    _offline_daemon(monkeypatch, started)
    monkeypatch.setattr(model_store, "resolve_model_path",
                        lambda *a, **k: tmp_path / "model.onnx")
    Transport(TryaiiDreConfig(data_dir=tmp_path))._warm_up()
    assert started == [1]
