"""The anthropic wrapper (TS-169): Messages API create / stream, sync and async.

Everything runs against duck-typed fakes and an injected ``route_fn`` (no
anthropic import, no daemon, no network), like test_monitor_wrap.py. One
test drives the real ``anthropic`` SDK over an in-memory HTTP transport in a
subprocess; it skips when the SDK is not installed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from importlib.util import find_spec
from pathlib import Path

import pytest

import tryaii.monitor as tm
from tryaii.monitor import store
from tryaii.monitor import wrapper_anthropic as wa
from tryaii.monitor import wrapper_openai as wo

NOW = datetime(2026, 10, 21, 9, 30, 0, 250000, tzinfo=timezone.utc)
SYSTEM = "You are terse."
MESSAGES = [{"role": "user", "content": "Fix the failing test in checkout.py"}]
CLASSIFICATION = {"broadCategory": "coding", "subcategory": "debugging", "confidence": 0.8123}
EMBEDDING = {"model": "fake-embed", "dim": 3, "v": [0.1234, -0.5, 1]}
SCORES = [
    {"modelId": "anthropic/claude-haiku-4.5", "qualityScore": 0.81},
    {"modelId": "anthropic/claude-sonnet-4.5", "qualityScore": 0.82},
    {"modelId": "openai/gpt-5-mini", "qualityScore": 0.70},
]
RAW_USAGE = {
    "input_tokens": 12, "output_tokens": 40,
    "cache_read_input_tokens": 2048, "cache_creation_input_tokens": 1500,
    "cache_creation": {"ephemeral_5m_input_tokens": 1000, "ephemeral_1h_input_tokens": 500},
    "server_tool_use": None, "service_tier": "standard",
}
EXPECTED_USAGE = {
    "prompt_tokens": 12, "completion_tokens": 40, "total_tokens": 52,
    "cached_tokens": 2048, "cache_write_tokens": 1500,
    "cache_writes": [{"tokens": 1000, "ttl": "5m"}, {"tokens": 500, "ttl": "1h"}],
    "reasoning_tokens": None,
}


# --- fakes ------------------------------------------------------------------

class Obj:
    """An attribute bag standing in for the SDK's pydantic models."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeUsage(Obj):
    def model_dump(self):
        return dict(self.__dict__)


def usage_obj(raw=RAW_USAGE):
    return FakeUsage(**raw)


def stream_events(start_usage=None, delta_usage=None):
    start = start_usage or {"input_tokens": 12, "output_tokens": 1,
                            "cache_read_input_tokens": 2048,
                            "cache_creation_input_tokens": 1500,
                            "cache_creation": {"ephemeral_5m_input_tokens": 1000,
                                               "ephemeral_1h_input_tokens": 500}}
    delta = delta_usage or {"output_tokens": 40, "input_tokens": None,
                            "cache_read_input_tokens": None,
                            "cache_creation_input_tokens": None}
    return [
        Obj(type="message_start", message=Obj(usage=FakeUsage(**start))),
        Obj(type="content_block_delta", delta=Obj(text="a")),
        Obj(type="message_delta", usage=FakeUsage(**delta)),
        Obj(type="message_stop"),
    ]


class FakeStream:
    def __init__(self, events):
        self._events = list(events)
        self.closed = False

    def __iter__(self):
        return iter(self._events)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def close(self):
        self.closed = True


class FakeAsyncStream:
    def __init__(self, events):
        self._events = list(events)

    def __aiter__(self):
        async def gen():
            for e in self._events:
                yield e
        return gen()

    async def aclose(self):
        pass


class FakeMessageStream:
    """``messages.stream()``'s MessageStream: the accumulated snapshot."""

    def __init__(self, usage):
        self._usage = usage

    @property
    def current_message_snapshot(self):
        if self._usage is None:
            raise AssertionError("no snapshot yet")
        return Obj(usage=self._usage)

    def get_final_text(self):
        return "done"


class FakeManager:
    def __init__(self, usage, raise_exc=None):
        self.usage = usage
        self.raise_exc = raise_exc
        self.exited = False

    def __enter__(self):
        if self.raise_exc is not None:
            raise self.raise_exc
        return FakeMessageStream(self.usage)

    def __exit__(self, *exc):
        self.exited = True
        return False


class FakeAsyncManager(FakeManager):
    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, *exc):
        return self.__exit__(*exc)


class OverloadedError(Exception):
    status_code = 529


class FakeMessages:
    def __init__(self, *, raise_exc=None, usage=True):
        self.calls = []
        self.raise_exc = raise_exc
        self.usage = usage

    def create(self, **kwargs):
        self.calls.append(dict(kwargs))
        if self.raise_exc is not None:
            raise self.raise_exc
        if kwargs.get("stream"):
            return FakeStream(stream_events())
        return Obj(model=kwargs.get("model"), usage=usage_obj() if self.usage else None)

    def stream(self, **kwargs):
        self.calls.append(dict(kwargs, _helper=True))
        return FakeManager(usage_obj() if self.usage else None, self.raise_exc)


class FakeAsyncMessages(FakeMessages):
    async def create(self, **kwargs):  # type: ignore[override]
        self.calls.append(dict(kwargs))
        if self.raise_exc is not None:
            raise self.raise_exc
        if kwargs.get("stream"):
            return FakeAsyncStream(stream_events())
        return Obj(model=kwargs.get("model"), usage=usage_obj())

    def stream(self, **kwargs):  # type: ignore[override]
        self.calls.append(dict(kwargs, _helper=True))
        return FakeAsyncManager(usage_obj(), self.raise_exc)


class FakeAnthropic:
    def __init__(self, messages=None, base_url="https://api.anthropic.com"):
        self.base_url = base_url
        self.messages = messages or FakeMessages()


def make_route_fn(calls=None):
    def route_fn(text):
        if calls is not None:
            calls.append(text)
        return SCORES, CLASSIFICATION, EMBEDDING
    return route_fn


def events_of(monitor):
    return store.read_recent_events(monitor.dir, 50)


@pytest.fixture(autouse=True)
def _pinned(monkeypatch, tmp_path):
    monkeypatch.setenv("TRYAII_DRE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TRYAII_MONITOR_MODE", raising=False)
    monkeypatch.delenv("TRYAII_APP", raising=False)
    monkeypatch.delenv("TRYAII_MONITOR_VECTORS", raising=False)
    monkeypatch.setattr(wo, "_utcnow", lambda: NOW)
    yield
    wo._defaults.update({"app": None, "process": None, "mode": None, "cachelint": False})


def call(client, **extra):
    kwargs = {"model": "claude-sonnet-4-5", "max_tokens": 256, "system": SYSTEM,
              "messages": MESSAGES}
    kwargs.update(extra)
    return client.messages.create(**kwargs)


# --- pure helpers --------------------------------------------------------------

def test_usage_maps_tokens_and_every_cache_field():
    assert wa.usage_from_anthropic(RAW_USAGE) == EXPECTED_USAGE
    assert wa.usage_from_anthropic(usage_obj()) == EXPECTED_USAGE


def test_usage_records_only_what_was_reported():
    assert wa.usage_from_anthropic(None) is None
    plain = wa.usage_from_anthropic({"input_tokens": 5, "output_tokens": 7})
    assert plain == {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12,
                     "cached_tokens": None, "cache_write_tokens": None, "cache_writes": None,
                     "reasoning_tokens": None}
    zero = wa.usage_from_anthropic({"input_tokens": 5, "output_tokens": 7,
                                    "cache_read_input_tokens": 0,
                                    "cache_creation_input_tokens": 0,
                                    "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                                       "ephemeral_1h_input_tokens": 0}})
    assert zero["cached_tokens"] == 0 and zero["cache_write_tokens"] == 0
    assert zero["cache_writes"] == []
    # No output count: no total either (never derived from one side).
    assert wa.usage_from_anthropic({"input_tokens": 5})["total_tokens"] is None


def test_stream_usage_merges_start_and_delta():
    merged = wa.merge_stream_usage(None, {"input_tokens": 3, "output_tokens": 1,
                                          "cache_read_input_tokens": 9})
    merged = wa.merge_stream_usage(merged, {"output_tokens": 30, "input_tokens": None})
    assert merged == {"input_tokens": 3, "output_tokens": 30, "cache_read_input_tokens": 9}


def test_routing_messages_put_system_first():
    blocks = [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}]
    assert wa.routing_messages({"system": blocks, "messages": MESSAGES})[0] == {
        "role": "system", "content": blocks}
    assert wa.routing_messages({"messages": MESSAGES}) == MESSAGES


def test_cache_ttl_policy_is_the_longest_explicit_ttl():
    assert wa.cache_ttl_policy({"messages": MESSAGES}) is None
    assert wa.cache_ttl_policy({"system": [
        {"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}]}) is None
    assert wa.cache_ttl_policy({
        "tools": [{"name": "t", "cache_control": {"type": "ephemeral", "ttl": "5m"}}],
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "x", "cache_control": {"type": "ephemeral", "ttl": "1h"}}]}],
    }) == "1h"
    assert wa.cache_ttl_policy({"cache_control": {"type": "ephemeral", "ttl": "5m"}}) == "5m"


@pytest.mark.parametrize("model,expected", [
    ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "claude-haiku-4-5-20251001"),
    ("anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4-5-20250929"),
    ("global.anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4-5-20250929"),
    ("claude-haiku-4-5@20251001", "claude-haiku-4-5@20251001"),
    ("claude-sonnet-4-5", "claude-sonnet-4-5"),
])
def test_bedrock_ids_lose_prefix_and_version(model, expected):
    assert wa.anchor_model_id(model) == expected


# --- create --------------------------------------------------------------------

def test_off_records_event_without_touching_the_call():
    routed = []
    client = FakeAnthropic()
    monitor = tm.wrap(client, app="shop", process="checkout", route_fn=make_route_fn(routed))
    assert isinstance(monitor, wa.AnthropicMonitor)
    resp = call(client, tryaii_process="refunds")

    assert resp.model == "claude-sonnet-4-5"
    sent = client.messages.calls[0]
    assert "tryaii_process" not in sent and "stream_options" not in sent
    assert sent["model"] == "claude-sonnet-4-5" and sent["system"] == SYSTEM
    [event] = events_of(monitor)
    assert event["process"] == "refunds"
    assert event["provider"] == "anthropic"
    assert event["model_requested"] == "claude-sonnet-4-5"
    assert event["model_resolved"] == "anthropic/claude-sonnet-4.5"
    assert event["model_used"] == "claude-sonnet-4-5"
    assert event["mode"] == "off" and event["route_status"] == "ok"
    assert event["winner"] == "anthropic/claude-haiku-4.5"
    assert event["winner_reachable"] is False   # never swapped on this client
    assert event["usage"] == EXPECTED_USAGE
    assert event["stream"] is False and event["error"] is None
    assert event["call_site"]["file"].endswith("test_monitor_anthropic.py")
    assert event["call_site"]["function"] == "call"
    assert routed == [SYSTEM + "\n\n" + MESSAGES[0]["content"]]
    assert event["vector"] is True


def test_event_provider_anthropic_shape_and_request_ttl():
    # provider "anthropic" tells the server prompt_tokens excludes the cache
    # (SPEC.md §3.1b); routing still treats it like class "other".
    client = FakeAnthropic()
    monitor = tm.wrap(client, app="shop", route_fn=make_route_fn())
    assert monitor.provider == "anthropic"
    assert monitor.usage_shape() == "anthropic"
    call(client, system=[{"type": "text", "text": SYSTEM,
                          "cache_control": {"type": "ephemeral", "ttl": "1h"}}])
    [event] = events_of(monitor)
    assert event["provider"] == "anthropic"
    assert event["cache_ttl_policy"] == "1h"
    assert event["usage"] == EXPECTED_USAGE


def test_dynamic_and_managed_never_swap_the_model():
    client = FakeAnthropic()
    monitor = tm.wrap(client, app="shop", mode="dynamic", route_fn=make_route_fn())
    call(client)
    assert client.messages.calls[0]["model"] == "claude-sonnet-4-5"
    [event] = events_of(monitor)
    assert event["mode"] == "dynamic" and event["model_used"] == "claude-sonnet-4-5"

    managed = FakeAnthropic()
    monitor = tm.wrap(managed, app="shop", route_fn=make_route_fn())
    store.write_config(monitor.dir, {
        "schema": "tryaii.monitor.config/1", "source": "local", "default_mode": "off",
        "processes": {"p": {"mode": "managed", "model": "anthropic/claude-haiku-4.5",
                            "providers": None}}})
    call(managed, tryaii_process="p")
    assert managed.messages.calls[0]["model"] == "claude-sonnet-4-5"
    managed_event = events_of(monitor)[-1]
    assert managed_event["mode"] == "managed"
    assert managed_event["route_status"] == "unreachable_managed"


def test_bedrock_model_id_resolves_its_anchor():
    client = FakeAnthropic(base_url="https://bedrock-runtime.us-east-1.amazonaws.com")
    monitor = tm.wrap(client, app="shop", route_fn=make_route_fn())
    call(client, model="us.anthropic.claude-haiku-4-5-20251001-v1:0")
    [event] = events_of(monitor)
    assert event["provider"] == "anthropic"
    assert event["model_requested"] == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert event["model_used"] == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert event["model_resolved"] == "anthropic/claude-haiku-4.5"


def test_provider_error_is_reraised_unchanged_and_recorded():
    exc = OverloadedError("busy")
    client = FakeAnthropic(FakeMessages(raise_exc=exc))
    monitor = tm.wrap(client, app="shop", route_fn=make_route_fn())
    with pytest.raises(OverloadedError) as info:
        call(client)
    assert info.value is exc
    [event] = events_of(monitor)
    assert event["error"] == {"status": 529, "class": "OverloadedError"}
    assert event["usage"] is None and event["route_status"] == "skipped"


def test_missing_usage_is_null():
    client = FakeAnthropic(FakeMessages(usage=False))
    monitor = tm.wrap(client, app="shop", route_fn=make_route_fn())
    call(client)
    [event] = events_of(monitor)
    assert event["usage"] is None


def test_stream_true_is_forwarded_and_recorded_at_the_end():
    client = FakeAnthropic()
    monitor = tm.wrap(client, app="shop", route_fn=make_route_fn())
    stream = call(client, stream=True)
    assert events_of(monitor) == []
    kinds = [e.type for e in stream]
    assert kinds == ["message_start", "content_block_delta", "message_delta", "message_stop"]
    [event] = events_of(monitor)
    assert event["stream"] is True
    assert event["usage"] == EXPECTED_USAGE


def test_stream_closed_early_records_what_arrived():
    client = FakeAnthropic()
    monitor = tm.wrap(client, app="shop", route_fn=make_route_fn())
    with call(client, stream=True) as stream:
        next(iter(stream))
    [event] = events_of(monitor)
    # Only message_start arrived: its usage, nothing guessed for the rest.
    assert event["usage"]["prompt_tokens"] == 12
    assert event["usage"]["completion_tokens"] == 1
    assert event["usage"]["cached_tokens"] == 2048


def test_stream_helper_records_the_accumulated_message():
    client = FakeAnthropic()
    monitor = tm.wrap(client, app="shop", route_fn=make_route_fn())
    with client.messages.stream(model="claude-sonnet-4-5", max_tokens=8, system=SYSTEM,
                                messages=MESSAGES, tryaii_process="helper") as stream:
        assert stream.get_final_text() == "done"
        assert events_of(monitor) == []
    assert "tryaii_process" not in client.messages.calls[0]
    [event] = events_of(monitor)
    assert event["stream"] is True and event["process"] == "helper"
    assert event["usage"] == EXPECTED_USAGE


def test_stream_helper_failure_is_recorded():
    exc = OverloadedError("busy")
    client = FakeAnthropic(FakeMessages(raise_exc=exc))
    monitor = tm.wrap(client, app="shop", route_fn=make_route_fn())
    with pytest.raises(OverloadedError):
        with client.messages.stream(model="claude-sonnet-4-5", max_tokens=8,
                                    messages=MESSAGES):
            pass
    [event] = events_of(monitor)
    assert event["error"] == {"status": 529, "class": "OverloadedError"}


# --- async ---------------------------------------------------------------------

async def test_async_create_and_stream():
    client = FakeAnthropic(FakeAsyncMessages())
    monitor = tm.wrap(client, app="shop", mode="dynamic", route_fn=make_route_fn())
    resp = await call(client)
    assert resp.model == "claude-sonnet-4-5"
    stream = await call(client, stream=True)
    kinds = [e.type async for e in stream]
    assert kinds[0] == "message_start" and kinds[-1] == "message_stop"
    events = events_of(monitor)
    assert [e["stream"] for e in events] == [False, True]
    assert all(e["usage"] == EXPECTED_USAGE for e in events)
    assert all(e["call_site"]["function"] == "call" for e in events)


async def test_async_stream_helper():
    client = FakeAnthropic(FakeAsyncMessages())
    monitor = tm.wrap(client, app="shop", route_fn=make_route_fn())
    async with client.messages.stream(model="claude-sonnet-4-5", max_tokens=8,
                                      messages=MESSAGES) as stream:
        assert stream.get_final_text() == "done"
    [event] = events_of(monitor)
    assert event["stream"] is True and event["usage"] == EXPECTED_USAGE
    assert event["call_site"]["function"] == "test_async_stream_helper"


# --- wrap / install --------------------------------------------------------------

def test_rewrap_replaces_the_previous_monitor():
    client = FakeAnthropic()
    first = tm.wrap(client, app="one", route_fn=make_route_fn())
    second = tm.wrap(client, app="two", route_fn=make_route_fn())
    call(client)
    assert events_of(first) == [] and len(events_of(second)) == 1
    assert len(client.messages.calls) == 1


def test_wrap_still_dispatches_openai_clients():
    class Completions:
        def create(self, **kwargs):
            return Obj(model=kwargs["model"], usage=None)

    client = Obj(base_url="https://api.openai.com/v1", chat=Obj(completions=Completions()))
    monitor = tm.wrap(client, app="shop", route_fn=make_route_fn())
    assert type(monitor) is wo.Monitor and monitor.provider == "openai"


def test_monitor_is_fail_open(monkeypatch):
    client = FakeAnthropic()
    monitor = tm.wrap(client, app="shop", route_fn=make_route_fn())
    monkeypatch.setattr(monitor, "_transport", None)   # every monitor step breaks
    assert call(client).model == "claude-sonnet-4-5"


def test_install_without_anthropic_returns_false(monkeypatch):
    monkeypatch.delitem(sys.modules, "anthropic", raising=False)
    assert wa.install_anthropic() is False


# --- the real SDK (subprocess: install() patches its classes) ---------------------

REAL_SDK = textwrap.dedent('''
    import asyncio, json, sys
    import anthropic
    try:  # newer SDKs ship their own fork of httpx
        import httpx2 as httpx
    except ImportError:
        import httpx
    from tryaii.monitor import install, store, wrap
    from tryaii.monitor import wrapper_anthropic as wa

    USAGE = {"input_tokens": 12, "output_tokens": 40, "cache_read_input_tokens": 2048,
             "cache_creation_input_tokens": 1500,
             "cache_creation": {"ephemeral_5m_input_tokens": 1000,
                                "ephemeral_1h_input_tokens": 500}}
    MESSAGE = {"id": "msg_1", "type": "message", "role": "assistant",
               "model": "claude-sonnet-4-6", "content": [{"type": "text", "text": "ok"}],
               "stop_reason": "end_turn", "stop_sequence": None, "usage": USAGE}

    def sse():
        start = dict(MESSAGE, content=[], stop_reason=None, usage=dict(USAGE, output_tokens=1))
        events = [
            ("message_start", {"type": "message_start", "message": start}),
            ("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": "ok"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta",
                               "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                               "usage": {"output_tokens": 40}}),
            ("message_stop", {"type": "message_stop"}),
        ]
        return "".join(f"event: {k}\\ndata: {json.dumps(v)}\\n\\n" for k, v in events)

    def handler(request):
        body = json.loads(request.content)
        if body.get("stream"):
            return httpx.Response(200, text=sse(),
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=MESSAGE)

    async def ahandler(request):
        return handler(request)

    route = lambda text: ([], None, None)
    kw = dict(model="claude-sonnet-4-6", max_tokens=16, system="Be terse.",
              messages=[{"role": "user", "content": "hi"}])
    out = {}

    client = anthropic.Anthropic(api_key="test",
                                 http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    mon = wrap(client, app="real", route_fn=route)
    client.messages.create(**kw)
    for _ in client.messages.create(stream=True, **kw):
        pass
    with client.messages.stream(**kw) as s:
        s.get_final_message()

    async def run_async():
        aclient = anthropic.AsyncAnthropic(
            api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(ahandler)))
        wrap(aclient, app="real", route_fn=route)
        await aclient.messages.create(**kw)
        async for _ in await aclient.messages.create(stream=True, **kw):
            pass
        async with aclient.messages.stream(**kw) as s:
            await s.get_final_message()
    asyncio.run(run_async())
    mon.transport.drain(5)
    out["wrapped"] = [[e["stream"], e["usage"], e["provider"], e["model_resolved"]]
                      for e in store.read_recent_events(mon.dir, 50)]

    # install(): every client made afterwards, Bedrock included, is monitored
    # (on an inline transport: no daemon is started).
    from tryaii.monitor import wrapper_openai as wo
    from tryaii.monitor.transport import Transport
    inline = Transport(route_fn=route, inline=True)
    inline.start()
    wo.get_transport = lambda: inline
    out["installed"] = install(app="inst")
    bedrock = anthropic.AnthropicBedrock(
        aws_region="us-east-1", aws_access_key="a", aws_secret_key="b",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    bedrock.messages.create(**dict(kw, model="us.anthropic.claude-haiku-4-5-20251001-v1:0"))
    bmon = wa._monitor_for(bedrock.messages)
    bmon.transport.drain(10)
    out["bedrock"] = [[e["usage"], e["model_resolved"]]
                      for e in store.read_recent_events(bmon.dir, 50)]
    print(json.dumps(out))
''')


@pytest.mark.skipif(find_spec("anthropic") is None,
                    reason="the anthropic SDK is not installed (pip install tryaii[anthropic])")
def test_real_sdk_sync_async_stream_and_install(tmp_path):
    env = dict(os.environ, TRYAII_DRE_DATA_DIR=str(tmp_path), TRYAII_MONITOR_VECTORS="0")
    env.pop("TRYAII_MONITOR_MODE", None)
    root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(p for p in (root, env.get("PYTHONPATH")) if p)
    proc = subprocess.run([sys.executable, "-c", REAL_SDK], env=env, cwd=str(tmp_path),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["wrapped"] == [[s, EXPECTED_USAGE, "anthropic", "anthropic/claude-sonnet-4.6"]
                              for s in (False, True, True, False, True, True)]
    assert out["installed"] is True
    assert out["bedrock"] == [[EXPECTED_USAGE, "anthropic/claude-haiku-4.5"]]
