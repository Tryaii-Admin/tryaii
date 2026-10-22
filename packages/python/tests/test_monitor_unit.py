"""Unit tests for the monitor pure core and store (shared/monitor/SPEC.md).

Fixture-frozen behaviour lives in test_monitor_fixtures.py; this file covers
what fixtures cannot: truncation at the router's limit, the store's file
layout and append/read round trips, vector de-duplication, timestamp
formatting and key order of the emitted records.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from tryaii.classifiers.base import MAX_PROMPT_LENGTH
from tryaii.monitor import (
    build_event,
    build_vector,
    format_ts,
    parse_ts,
    pick_winner,
    provider_class,
    reachable,
    resolve_app,
    routing_text,
    set_pin,
    store,
)
from tryaii.monitor.events import (
    CACHE_TTLS,
    classification_from_wire,
    normalize_cache_usage,
    normalize_usage,
    usage_from_response,
)
from tryaii.monitor.extract import prompt_hash

# --- extract ----------------------------------------------------------------

def test_routing_text_truncates_to_router_limit():
    text, digest = routing_text([{"role": "user", "content": "x" * (MAX_PROMPT_LENGTH + 10)}])
    assert len(text) == MAX_PROMPT_LENGTH
    assert digest == prompt_hash("x" * MAX_PROMPT_LENGTH)


def test_routing_text_is_deterministic():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    assert routing_text(msgs) == routing_text(list(msgs))
    assert routing_text(msgs)[0] == "s\n\nu"


# --- timestamps -------------------------------------------------------------

def test_format_and_parse_ts_round_trip():
    dt = datetime(2026, 10, 11, 12, 0, 0, 123456, tzinfo=timezone.utc)
    text = format_ts(dt)
    assert text == "2026-10-11T12:00:00.123Z"
    assert parse_ts(text) == dt.replace(microsecond=123000)
    assert parse_ts("2026-10-11T12:00:00Z") == dt.replace(microsecond=0)
    assert parse_ts("2026-10-11T12:00:00.123+00:00") == dt.replace(microsecond=123000)
    with pytest.raises(ValueError):
        parse_ts("yesterday")


def test_format_ts_converts_offsets_to_utc():
    from datetime import timedelta

    dt = datetime(2026, 10, 11, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    assert format_ts(dt) == "2026-10-11T12:00:00.000Z"


# --- provider / winner ------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    (None, "openai"), ("", "openai"), ("https://api.openai.com/v1", "openai"),
    ("https://openrouter.ai/api/v1/", "openrouter"), ("http://127.0.0.1:8080/v1", "other"),
    ("https://myco.openai.azure.com/openai", "other"), ("not a url", "other"),
])
def test_provider_class(url, expected):
    assert provider_class(url) == expected


def test_reachable_rules():
    assert reachable("openrouter", "anthropic/claude-sonnet-4.5")
    assert reachable("openai", "openai/gpt-5.4")
    assert not reachable("openai", "anthropic/claude-sonnet-4.5")
    assert reachable("other", "openai/gpt-5.5", "openai/gpt-5.5")
    assert not reachable("other", "openai/gpt-5.4", "openai/gpt-5.5")
    assert not reachable("openrouter", None)


def test_pick_winner_accepts_model_score_objects():
    from tryaii.catalog.bundle import starter_bundle
    from tryaii.registry.models import ModelRegistry
    from tryaii.scoring.engine import ModelScore

    registry = ModelRegistry.from_bundle(starter_bundle())

    def score(model_id, quality):
        return ModelScore(model_id=model_id, final_score=0.9, quality_score=quality,
                          cost_score=0.5, speed_score=0.5, quality_contribution=0,
                          cost_contribution=0, speed_contribution=0, top_benchmarks=[],
                          reasoning="")

    ranked = [score("openai/gpt-5.4", 0.80), score("openai/gpt-5.5", 0.82)]
    assert pick_winner(ranked, "openai/gpt-5.5", registry) == {
        "winner": "openai/gpt-5.4", "rank": 1, "method": "band"}


# --- events -----------------------------------------------------------------

def _event(**overrides):
    base = dict(ts="2026-10-11T12:00:00.000Z", app="shop", process="p",
                call_site={"file": "a.py", "line": 1, "function": "f", "extra": "dropped"},
                provider="openai", model_requested="gpt-5.5", model_resolved="openai/gpt-5.5",
                model_used="gpt-5.5", mode="off", winner=None, winner_reachable=False,
                route_status="ok", classification=None, usage=None, est_cost_usd=1.0,
                est_cost_winner_usd=0.12345678, latency_ms=12.7, stream=False, error=None,
                prompt_hash="h", vector=False)
    base.update(overrides)
    return build_event(**base)


def test_build_event_key_order_and_rounding():
    event = _event()
    assert list(event) == [
        "schema", "event_id", "ts", "app", "process", "call_site", "provider",
        "model_requested", "model_resolved", "model_used", "mode", "winner",
        "winner_reachable", "route_status", "classification", "taxonomy_version", "usage",
        "cache_ttl_policy",
        "est_cost_usd", "est_cost_winner_usd", "est_cost_anchor_usd", "latency_ms",
        "stream", "error",
        "prompt_hash", "vector"]
    assert event["cache_ttl_policy"] is None
    assert event["call_site"] == {"file": "a.py", "line": 1, "function": "f"}
    assert event["est_cost_usd"] == 1 and isinstance(event["est_cost_usd"], int)
    assert event["est_cost_winner_usd"] == 0.123457
    assert event["latency_ms"] == 12


def test_build_vector_rounds_and_counts():
    rec = build_vector(prompt_hash="h", ts="2026-10-11T12:00:00.000Z", process="p",
                       model="m", vector=[0.12345, 1.0, -0.5])
    assert list(rec) == ["schema", "prompt_hash", "ts", "process", "model", "dim", "v"]
    assert rec["dim"] == 3 and rec["v"] == [0.1235, 1, -0.5]


def test_usage_and_classification_normalisation():
    class Usage:
        prompt_tokens = 10
        completion_tokens = 2
        total_tokens = 12

    assert usage_from_response(Usage()) == {
        "prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12,
        "cached_tokens": None, "cache_write_tokens": None, "cache_writes": None}
    assert usage_from_response({"prompt_tokens": 3}) == {
        "prompt_tokens": 3, "completion_tokens": None, "total_tokens": None,
        "cached_tokens": None, "cache_write_tokens": None, "cache_writes": None}
    assert usage_from_response({}) is None
    wire = {"broadCategory": "coding", "subcategory": "x", "confidence": 0.123456}
    assert classification_from_wire(wire) == {
        "category": "coding", "subcategory": "x", "confidence": 0.1235}
    assert classification_from_wire(None) is None


# --- prompt-cache usage (SPEC.md §3.1b, TS-228) ------------------------------

def test_build_event_carries_cache_usage_in_contract_order():
    usage = {"total_tokens": 12, "cache_writes": [{"ttl": "1H", "tokens": 5.0},
                                                  {"ttl": "5m", "tokens": 0}],
             "prompt_tokens": 10, "cached_tokens": True, "completion_tokens": 2}
    event = _event(usage=usage, cache_ttl_policy="1h")
    assert list(event["usage"]) == ["prompt_tokens", "completion_tokens", "total_tokens",
                                    "cached_tokens", "cache_write_tokens", "cache_writes"]
    # a boolean is not a count; the aggregate is summed from the breakdown; a
    # zero entry is dropped; the ttl is lower-cased; an integral float is an int
    assert event["usage"]["cached_tokens"] is None
    assert event["usage"]["cache_write_tokens"] == 5
    assert event["usage"]["cache_writes"] == [{"tokens": 5, "ttl": "1h"}]
    assert event["cache_ttl_policy"] == "1h"
    assert _event(cache_ttl_policy="2h")["cache_ttl_policy"] is None
    assert _event(cache_ttl_policy=" EXPLICIT ")["cache_ttl_policy"] == "explicit"
    assert _event(usage={"cache_writes": "nope"})["usage"] is None
    assert CACHE_TTLS == ("5m", "1h", "30m", "24h", "explicit")


def test_normalize_usage_keeps_zero_and_drops_negatives():
    out = normalize_usage({"prompt_tokens": 1, "cached_tokens": 0,
                           "cache_write_tokens": -1, "cache_writes": []})
    assert out == {"prompt_tokens": 1, "completion_tokens": None, "total_tokens": None,
                   "cached_tokens": 0, "cache_write_tokens": 0, "cache_writes": []}


def test_normalize_cache_usage_reads_objects_like_dicts():
    class Details:
        cached_tokens = 1024
        cache_write_tokens = None

    class Usage:
        prompt_tokens = 1200
        prompt_tokens_details = Details()
        cache_write_tokens = 2048

    assert normalize_cache_usage("OpenAI", Usage()) == {
        "cached_tokens": 1024, "cache_write_tokens": 2048, "cache_writes": None}
    assert normalize_cache_usage("anthropic", None) == {
        "cached_tokens": None, "cache_write_tokens": None, "cache_writes": None}
    assert normalize_cache_usage(None, {"cached_tokens": 1}) == {
        "cached_tokens": None, "cache_write_tokens": None, "cache_writes": None}


# --- pins / config ----------------------------------------------------------

def test_set_pin_sorts_and_clears():
    pins = set_pin(None, "b", {"model": "openai/gpt-5.4"})
    pins = set_pin(pins, "a", {"model": "openai/gpt-5-mini"})
    assert list(pins["processes"]) == ["a", "b"]
    assert set_pin(pins, "a", None)["processes"] == {"b": {"model": "openai/gpt-5.4"}}


def test_resolve_app_sanitizes_cwd():
    assert resolve_app(env={}, args=None, cwd_name="My Repo!") == {
        "app": "my-repo", "source": "cwd"}


# --- store ------------------------------------------------------------------

def test_store_layout_and_round_trip(tmp_path):
    app = store.app_dir(tmp_path, "shop")
    assert app == tmp_path / "monitor" / "shop"
    e1 = _event(ts="2026-10-11T23:59:59.000Z", prompt_hash="a")
    e2 = _event(ts="2026-10-12T00:00:01.000Z", prompt_hash="b")
    store.append_event(app, e1)
    store.append_event(app, e2)
    assert sorted(p.name for p in app.iterdir()) == [
        "events-2026-10-11.jsonl", "events-2026-10-12.jsonl"]
    raw = (app / "events-2026-10-11.jsonl").read_bytes()
    assert raw == (json.dumps(e1, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    assert store.read_events(app, "2026-10-12T00:00:30.000Z", 3600) == [e1, e2]
    assert store.read_events(app, "2026-10-12T00:00:30.000Z", 30) == [e2]
    assert store.read_recent_events(app, 1) == [e2]
    assert store.read_recent_events(app, 5) == [e1, e2]
    assert store.list_apps(tmp_path) == ["shop"]


def test_store_skips_malformed_lines(tmp_path):
    app = store.app_dir(tmp_path, "shop")
    app.mkdir(parents=True)
    good = _event(ts="2026-10-11T12:00:00.000Z")
    (app / "events-2026-10-11.jsonl").write_text(
        "not json\n" + json.dumps(good) + "\n" + json.dumps({"ts": "bad"}) + "\n[1]\n",
        encoding="utf-8")
    assert store.read_events(app, "2026-10-11T13:00:00.000Z", 86400) == [good]


def test_vectors_deduplicate_per_day(tmp_path):
    app = store.app_dir(tmp_path, "shop")
    rec = build_vector(prompt_hash="h", ts="2026-10-11T12:00:00.000Z", process="p", model="m",
                       vector=[1, 2])
    assert store.append_vector(app, rec) is True
    assert store.append_vector(app, dict(rec, ts="2026-10-11T13:00:00.000Z")) is False
    assert store.append_vector(app, dict(rec, ts="2026-10-12T13:00:00.000Z")) is True
    assert len((app / "vectors-2026-10-11.jsonl").read_text().splitlines()) == 1
    # a fresh process sees the hashes already on disk
    store._seen_hashes.clear()
    assert store.append_vector(app, rec) is False


def test_pins_and_config_documents(tmp_path):
    app = store.app_dir(tmp_path, "shop")
    assert store.read_pins(app) == {"schema": "tryaii.monitor.pins/1", "processes": {}}
    assert store.read_config(app)["default_mode"] == "off"
    store.write_config(app, {"schema": "tryaii.monitor.config/1", "source": "local",
                             "default_mode": "off",
                             "processes": {"p": {"mode": "stable", "providers": None}}})
    text = (app / "config.json").read_text(encoding="utf-8")
    assert text.endswith("}\n") and '"mode": "stable"' in text
    assert store.read_config(app)["processes"]["p"]["mode"] == "stable"
    assert store.config_mtime(app) is not None
    (app / "pins.json").write_text("{broken", encoding="utf-8")
    assert store.read_pins(app)["processes"] == {}


def test_dumps_line_prints_numbers_like_node():
    from tryaii.monitor.store import dumps_line, js_numbers

    line = dumps_line({"a": 9e-05, "b": 1e-07, "c": 1e21, "d": 0.004213,
                       "n": 1, "x": -2.5e-05, "v": [1.0, 0.0001], "s": "3e5"})
    assert line == ('{"a":0.00009,"b":1e-7,"c":1e+21,"d":0.004213,"n":1,'
                    '"x":-0.000025,"v":[1,0.0001],"s":"3e5"}\n')
    # Number-like text inside strings is never touched.
    assert js_numbers('{"h":"1e-05 and 2.0"}') == '{"h":"1e-05 and 2.0"}'


# --- slice 2: event ids, taxonomy, managed, policy, remote, upload ----------

def test_event_id_is_a_fresh_uuid4_unless_given():
    import uuid

    a, b = _event(), _event()
    assert a["event_id"] != b["event_id"]
    assert uuid.UUID(a["event_id"]).version == 4
    assert _event(event_id="e-1")["event_id"] == "e-1"
    assert _event(taxonomy_version="2026.10")["taxonomy_version"] == "2026.10"


def test_taxonomy_version_reads_benchmarks_then_manifest():
    from types import SimpleNamespace

    from tryaii.catalog.bundle import starter_bundle
    from tryaii.monitor.events import taxonomy_version

    assert taxonomy_version(starter_bundle()) is None
    assert taxonomy_version(None) is None
    both = SimpleNamespace(benchmarks={"taxonomy_version": "t-b"},
                           manifest={"taxonomy_version": "t-m"})
    assert taxonomy_version(both) == "t-b"
    only_manifest = SimpleNamespace(benchmarks={}, manifest={"taxonomy_version": " t-m "})
    assert taxonomy_version(only_manifest) == "t-m"
    assert taxonomy_version(SimpleNamespace(benchmarks={"taxonomy_version": ""},
                                            manifest={})) is None


def test_set_process_mode_managed_needs_a_model_and_orders_keys():
    from tryaii.monitor.config import set_process_mode

    with pytest.raises(ValueError, match="managed mode needs --model <id>"):
        set_process_mode(None, "p", "managed")
    with pytest.raises(ValueError, match="mode must be off, stable, dynamic or managed"):
        set_process_mode(None, "p", "fast")
    doc = set_process_mode({"processes": {"p": {"providers": ["x"], "note": 1}}},
                           "p", "managed", " openai/gpt-5-mini ")
    assert list(doc["processes"]["p"]) == ["mode", "model", "providers", "note"]
    assert doc["processes"]["p"]["model"] == "openai/gpt-5-mini"
    back = set_process_mode(doc, "p", "off", "ignored")
    assert back["processes"]["p"] == {"mode": "off", "providers": ["x"], "note": 1}


def test_resolve_mode_carries_the_managed_model_of_its_level():
    from tryaii.monitor.config import resolve_mode

    remote = {"default_mode": "managed",
              "processes": {"a": {"mode": "managed", "model": "openai/gpt-5-mini"}}}
    assert resolve_mode("a", remote=remote) == {
        "mode": "managed", "source": "remote", "model": "openai/gpt-5-mini"}
    assert resolve_mode("b", remote=remote) == {
        "mode": "managed", "source": "remote", "model": None}
    assert resolve_mode("a", env={"TRYAII_MONITOR_MODE": "managed"}, remote=remote) == {
        "mode": "managed", "source": "env", "model": None}


def test_policy_allowed_fails_closed_without_origins():
    from tryaii.monitor.winner import policy_allowed
    from tryaii.policy import ModelPolicy

    assert policy_allowed(None, None, ["a"]) is None
    assert policy_allowed(ModelPolicy(), None, ["a"]) is None
    assert policy_allowed(ModelPolicy(origin="no-prc-models"), None, ["a"]) == []


def test_route_and_shadow_allowed_lists():
    from tryaii.monitor.winner import route_allowed, shadow_allowed

    ids = ["openai/a", "anthropic/b", "openai/c"]
    assert route_allowed("openrouter", ids, None, None) is None
    assert route_allowed("openrouter", ids, None, ["openai/c", "anthropic/b"]) == [
        "anthropic/b", "openai/c"]
    assert route_allowed("openai", ids, "openai/a", ["anthropic/b", "openai/c"]) == ["openai/c"]
    assert route_allowed("other", ids, "openai/a", None) == ["openai/a"]
    assert shadow_allowed(ids, None) is None
    assert shadow_allowed(ids, {"openai/c", "openai/a"}) == ["openai/a", "openai/c"]


def test_pick_winner_never_recommends_a_disallowed_model():
    from tryaii.catalog.bundle import starter_bundle
    from tryaii.registry.models import ModelRegistry

    registry = ModelRegistry.from_bundle(starter_bundle())
    ranked = [{"modelId": "openai/gpt-5-mini", "qualityScore": 0.80},
              {"modelId": "openai/gpt-5.5", "qualityScore": 0.82}]
    assert pick_winner(ranked, "openai/gpt-5.5", registry)["winner"] == "openai/gpt-5-mini"
    assert pick_winner(ranked, "openai/gpt-5.5", registry,
                       allowed=["openai/gpt-5.5"])["winner"] == "openai/gpt-5.5"
    assert pick_winner(ranked, "openai/gpt-5.5", registry, allowed=[]) == {
        "winner": None, "rank": None, "method": "none"}


def test_store_remote_and_upload_cursor_round_trip(tmp_path):
    assert store.read_remote(tmp_path) is None
    (tmp_path / "remote.json").write_text('{"schema": "other"}', encoding="utf-8")
    assert store.read_remote(tmp_path) is None
    store.write_remote(tmp_path, {"schema": "tryaii.monitor.remote/1", "etag": "e1",
                                  "fetched_at": "old"}, "2026-10-12T00:00:00.000Z")
    doc = store.read_remote(tmp_path)
    assert list(doc) == ["schema", "etag", "fetched_at"]
    assert doc["fetched_at"] == "2026-10-12T00:00:00.000Z"

    assert store.read_upload_cursor(tmp_path) is None
    store.write_upload_cursor(tmp_path, {"files": {"events-b.jsonl": 3, "events-a.jsonl": 5},
                                         "last_ok": None, "last_error": "network"})
    text = (tmp_path / "upload.json").read_text(encoding="utf-8")
    assert text.startswith('{\n  "schema": "tryaii.monitor.upload-cursor/1",')
    cursor = store.read_upload_cursor(tmp_path)
    assert list(cursor["files"]) == ["events-a.jsonl", "events-b.jsonl"]
    assert cursor["last_error"] == "network"


def test_uploaded_event_count_counts_lines_in_the_prefix(tmp_path):
    (tmp_path / "events-2026-10-12.jsonl").write_bytes(b'{"a":1}\n{"b":2}\n{"c":3}\n')
    cursor = {"files": {"events-2026-10-12.jsonl": 16, "events-gone.jsonl": 9}}
    assert store.uploaded_event_count(tmp_path, cursor) == 2
    assert store.uploaded_event_count(tmp_path, None) == 0


def test_status_source_and_upload_blocks():
    from tryaii.monitor.status import source_info, upload_info

    assert source_info(None) == {"kind": "local", "etag": None, "fetched_at": None}
    assert source_info({"etag": "e", "fetched_at": "t"}) == {
        "kind": "remote", "etag": "e", "fetched_at": "t"}
    assert upload_info(None) == {"state": "off", "events": 0, "last_ok": None,
                                 "last_error": None}
    assert upload_info({"last_ok": "t", "last_error": None}, 7)["state"] == "ok"
    assert upload_info({"last_ok": "t", "last_error": "http 503"}, 7)["state"] == "error"


def test_call_site_file_is_relative_to_the_working_directory(tmp_path):
    """TS-226: the record names the file inside the app, never the machine."""
    import os

    from tryaii.monitor import callsite

    cwd = str(tmp_path)
    inside = os.path.join(cwd, "src", "app.py")
    assert callsite.relative_file(inside, cwd) == "src/app.py"
    assert callsite.relative_file(os.path.join(cwd, "main.py"), cwd) == "main.py"
    outside = os.path.join(os.path.dirname(cwd), "elsewhere", "job.py")
    assert callsite.relative_file(outside, cwd) == "job.py"
    assert callsite.relative_file("already/relative.py", cwd) == "already/relative.py"
    assert callsite.relative_file("win\\style.py", cwd) == "win/style.py"
    assert callsite.relative_file("~/secret/app.py", cwd) == "app.py"
    assert callsite.relative_file(None, cwd) is None
    assert callsite.is_absolute_file("C:\\Users\\x\\a.py")
    assert callsite.is_absolute_file("/home/x/a.py")
    assert not callsite.is_absolute_file("src/a.py")
