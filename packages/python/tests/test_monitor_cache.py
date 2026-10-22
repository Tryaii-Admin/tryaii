"""Cache fields of the event (API.md v1.4 §3.2.1, SPEC.md §3.1).

The pure helpers are pinned cross-SDK by shared/monitor/fixtures/cache
(test_monitor_fixtures.py); this file covers the wrapper wiring: where the
block sits in the event, when it is absent, and that the local events file
never holds prompt text.
"""

from __future__ import annotations

import pytest

from tests.test_monitor_wrap import (  # noqa: F401 - _pinned is an autouse fixture
    NOW,
    SCORES_MINI_WINS,
    FakeClient,
    FakeCompletions,
    FakeResponse,
    _pinned,
    events_of,
    make_route_fn,
)
from tryaii.monitor import cache, store, wrap
from tryaii.monitor import wrapper_openai as wo
from tryaii.monitor.events import usage_from_response

pytest.importorskip("tiktoken")

STATIC = "Refund policy: items may be returned within thirty days of delivery. " * 110
SECRET_DATE = "2026-10-18 14:02"
SYSTEM = f"You are the support assistant. Now: {SECRET_DATE}. Customer {{{{sku}}}}. " + STATIC


def _messages(user="Where is my order?"):
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


class CachedUsage:
    def model_dump(self):
        return {"prompt_tokens": 1400, "completion_tokens": 10, "total_tokens": 1410,
                "prompt_tokens_details": {"cached_tokens": 1280}}


class CachedCompletions(FakeCompletions):
    def create(self, **kwargs):
        self.calls.append(dict(kwargs))
        return FakeResponse(kwargs.get("model"), CachedUsage())


def test_cached_tokens_recorded_without_cachelint():
    client = FakeClient(completions=CachedCompletions())
    monitor = wrap(client, app="shop", process="support",
                   route_fn=make_route_fn(SCORES_MINI_WINS))
    client.chat.completions.create(model="gpt-5.5", messages=_messages())
    [event] = events_of(monitor)
    assert event["usage"] == {"prompt_tokens": 1400, "completion_tokens": 10,
                              "total_tokens": 1410, "cached_tokens": 1280,
                              "cache_write_tokens": None, "cache_writes": None}
    assert "cache" not in event   # cachelint off: no cache block at all


def test_cache_block_is_the_last_key_and_never_holds_prompt_text(capsys):
    client = FakeClient()
    monitor = wrap(client, app="shop", process="support",
                   route_fn=make_route_fn(SCORES_MINI_WINS), cachelint=True)
    client.chat.completions.create(model="gpt-4o-mini", messages=_messages())
    client.chat.completions.create(model="gpt-4o-mini", messages=_messages("Can I return it?"))
    events = events_of(monitor)
    assert len(events) == 2
    for event in events:
        assert list(event)[-2:] == ["vector", "cache"]
    first, second = [e["cache"] for e in events]
    assert first["outcome"] is None              # first linted call of the process
    assert second["report"] is None              # < 60 s after the first report
    assert second["outcome"] == "partial"        # same prefix, new user turn
    report = first["report"]
    assert report["verdict"] == "EFFECTIVELY_UNCACHEABLE"
    assert [f["excerpt"] for f in report["findings"]] == ["0000-00-00 00:00", "{{xxx}}"]
    assert report["recommendation"].startswith("at messages[0]:system+")
    # The raw events file never holds the prompt's text.
    raw = "".join(path.read_text(encoding="utf-8") for path in store.event_files(monitor.dir))
    for secret in (SECRET_DATE, "{{sku}}", "Refund policy", "Where is my order"):
        assert secret not in raw
    capsys.readouterr()   # the hook's stderr warnings are not under test here


def test_tracker_report_cadence_and_outcomes():
    clock = {"t": 0.0}
    tracker = cache.CacheTracker(clock=lambda: clock["t"])
    a = tracker.observe("p", "gpt-4o-mini", _messages())
    assert a["outcome"] is None and a["report"] is not None
    clock["t"] = 30
    b = tracker.observe("p", "gpt-4o-mini", _messages("Another question"))
    assert b["report"] is None and b["outcome"] in ("hit", "partial", "miss")
    clock["t"] = 120
    later = tracker.observe("p", "gpt-4o-mini", _messages("Another question"))
    assert later["report"] is not None            # changed (token counts) and >= 60 s
    assert later["report"]["prompt_tokens"] != a["report"]["prompt_tokens"]
    clock["t"] = 130
    assert tracker.observe("p", "gpt-4o-mini", _messages("Third"))["report"] is None
    clock["t"] = 300
    same = tracker.observe("p", "gpt-4o-mini", _messages("Another question"))
    assert same["report"] is None                 # same as the last report sent
    other = tracker.observe("q", "gpt-4o-mini", _messages())
    assert other["outcome"] is None and other["report"] is not None   # per process


def test_full_analysis_runs_at_most_once_per_60s_per_process():
    """A per-call timestamp changes the prompt every call while the masked
    report stays the same: 20 calls 10 s apart must not analyse every call."""
    clock = {"t": 0.0}
    tracker = cache.CacheTracker(clock=lambda: clock["t"])
    reports = 0
    for i in range(20):
        clock["t"] = i * 10.0
        stamp = f"2026-10-18 14:{2 + i // 6:02d}:{(i * 10) % 60:02d}"
        msgs = [{"role": "system", "content": SYSTEM.replace(SECRET_DATE, stamp)},
                {"role": "user", "content": "Can I return shoes?"}]
        block = tracker.observe("p", "gpt-4o-mini", msgs)
        reports += block["report"] is not None
        if i:
            assert block["outcome"] == "miss"     # the pair prediction runs every call
    assert tracker.analyses == 4                  # t = 0, 60, 120, 180
    assert reports == 1                           # the masked report never changed
    tracker.observe("q", "gpt-4o-mini", _messages())
    assert tracker.analyses == 5                  # per process


TOOLS = [{"type": "function", "function": {
    "name": "lookup_order", "description": "Look up an order by its id.",
    "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}},
                   "required": ["order_id"]}}}]
FUNCTIONS = [{"name": "escalate", "parameters": {"type": "object", "properties": {}}}]


def test_lint_prompt_carries_tools_then_functions():
    assert cache.lint_prompt([]) == {"messages": []}
    assert cache.lint_prompt([], TOOLS, FUNCTIONS) == {"messages": [], "tools": TOOLS + FUNCTIONS}
    assert cache.lint_prompt([], None, FUNCTIONS) == {"messages": [], "tools": FUNCTIONS}
    assert cache.lint_prompt([], "bad", []) == {"messages": []}


def test_wrapper_lints_the_request_tool_schemas(capsys):
    client = FakeClient()
    monitor = wrap(client, app="shop", process="support",
                   route_fn=make_route_fn(SCORES_MINI_WINS), cachelint=True)
    client.chat.completions.create(model="gpt-4o-mini", messages=_messages(),
                                   tools=TOOLS, functions=FUNCTIONS)
    [event] = events_of(monitor)
    report = event["cache"]["report"]
    assert report["tools_tokens"] > 0
    assert report["prompt_tokens"] == (report["tools_tokens"] + report["system_tokens"]
                                       + report["messages_tokens"])
    raw = "".join(path.read_text(encoding="utf-8") for path in store.event_files(monitor.dir))
    assert "lookup_order" not in raw
    capsys.readouterr()


def test_failing_tracker_leaves_the_event_intact(monkeypatch):
    client = FakeClient()
    monitor = wrap(client, app="shop", process="support",
                   route_fn=make_route_fn(SCORES_MINI_WINS), cachelint=True)

    def boom(*_a, **_k):
        raise RuntimeError("engine broke")

    monkeypatch.setattr(monitor._cache, "observe", boom)
    client.chat.completions.create(model="gpt-4o-mini", messages=_messages())
    [event] = events_of(monitor)
    assert "cache" not in event and event["usage"]["cached_tokens"] is None


def test_snapshot_survives_the_caller_mutating_its_messages():
    client = FakeClient()
    monitor = wrap(client, app="shop", process="support",
                   route_fn=make_route_fn(SCORES_MINI_WINS), cachelint=True)
    plan = monitor.before({"model": "gpt-4o-mini", "messages": _messages()}, None, False)
    messages = _messages()
    monitor.apply(plan, {"model": "gpt-4o-mini", "messages": messages})
    messages[0]["content"] = "changed"
    assert plan.lint_messages[0]["content"] == SYSTEM


def test_usage_from_response_object_details():
    class Details:
        cached_tokens = 64

    class Usage:
        prompt_tokens = 100
        completion_tokens = 5
        total_tokens = 105
        prompt_tokens_details = Details()

    assert usage_from_response(Usage())["cached_tokens"] == 64
    assert wo.format_ts(NOW) == "2026-10-11T12:00:00.123Z"
