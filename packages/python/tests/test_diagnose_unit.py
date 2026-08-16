"""Unit tests for the diagnose engine — the edges fixtures can't carry.

Fixtures (test_diagnose_fixtures.py) pin behavior against the default
catalog; this suite covers custom-registry edges (ambiguous ids, missing
pricing), the classify_fn seam, the store, and opts handling. House style:
no mock libraries — hand-written fakes only.
"""

from __future__ import annotations

import json

import pytest

from tryaii.diagnose import (
    analyze_inventory,
    latest_run_id,
    list_run_ids,
    load_run_findings,
    previous_run_id,
    resolve_model_id,
    write_run,
)
from tryaii.diagnose.api import _read_discount_factors
from tryaii.diagnose.cost import run_cost
from tryaii.registry.models import ModelRegistry

_SIMS = {"MMLU": 0.6, "HumanEval": 0.3}


def _mini_registry() -> ModelRegistry:
    registry = ModelRegistry()
    registry.add("model-a", "TestCo", benchmarks={"MMLU": 85.0},
                 pricing=(0.001, 0.002), latency="fast")
    registry.add("model-b", "TestCo", benchmarks={"MMLU": 70.0},
                 pricing=(0.0001, 0.0002), latency="very fast")
    registry.add("free-model", "TestCo", benchmarks={"MMLU": 60.0})
    return registry


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------

def test_resolve_ambiguous_normalization_resolves_to_nothing():
    registry = ModelRegistry()
    registry.add("gpt-x.1", "TestCo")
    registry.add("gpt-x-1", "TestCo")
    # "gpt x 1" normalizes to "gpt-x-1", which matches BOTH ids after their
    # own normalization — SPEC §2.1 never guesses between two models.
    model_id, method = resolve_model_id("gpt x 1", registry)
    assert (model_id, method) == (None, "none")


def test_resolve_exact_wins_over_normalization():
    registry = ModelRegistry()
    registry.add("gpt-x.1", "TestCo")
    assert resolve_model_id("gpt-x.1", registry) == ("gpt-x.1", "exact")


# ---------------------------------------------------------------------------
# model_fit — recommended_same_price band edges (SPEC §2.2.1)
# ---------------------------------------------------------------------------

def _run_fit(registry, model_id):
    from tryaii.diagnose.modelfit import run_model_fit
    from tryaii.scoring.priorities import Priorities

    payload, _internal = run_model_fit(
        {"benchmark_similarities": _SIMS}, model_id, model_id,
        Priorities(5, 1, 1), registry)
    return payload["recommended_same_price"]


def test_same_price_picks_better_model_inside_band():
    registry = _mini_registry()
    # blended 0.0014 — inside model-a's ±20% band [0.0012, 0.0018] — and
    # higher quality, so it outranks model-a under quality-first priorities.
    registry.add("model-c", "TestCo", benchmarks={"MMLU": 95.0},
                 pricing=(0.0011, 0.0017), latency="fast")
    sp = _run_fit(registry, "model-a")
    assert sp["model_id"] == "model-c"
    assert sp["is_current"] is False


def test_same_price_excludes_out_of_band_models():
    # model-b (blended 0.00015) is far below model-a's band, so even though
    # it exists, the band search falls back to model-a itself.
    sp = _run_fit(_mini_registry(), "model-a")
    assert sp["model_id"] == "model-a"
    assert sp["is_current"] is True


def test_same_price_null_when_current_unpriced():
    assert _run_fit(_mini_registry(), "free-model") is None


def test_same_price_null_when_current_unresolved():
    from tryaii.diagnose.modelfit import run_model_fit
    from tryaii.scoring.priorities import Priorities

    payload, _internal = run_model_fit(
        {"benchmark_similarities": _SIMS}, None, "claude-9-mega",
        Priorities(5, 1, 1), _mini_registry())
    assert payload["recommended_same_price"] is None


# ---------------------------------------------------------------------------
# cost — custom-registry edges
# ---------------------------------------------------------------------------

def test_cost_no_pricing_is_insufficient():
    result = run_cost(
        canonical_text="user: hello",
        resolved_model_id="free-model",
        declared_model="free-model",
        registry=_mini_registry(),
        output_tokens=100,
        calls_per_day=10,
        cache_ctx=None,
        fit_internal=None,
        read_discount_factors=_read_discount_factors(),
    )
    assert result["status"] == "insufficient_data"
    assert result["reason"] == "no pricing for 'free-model'"
    assert result["input_tokens"] == 3  # the estimate is still itemized
    assert result["cost_per_call_usd"] is None


def test_cost_unknown_provider_key_has_no_savings():
    result = run_cost(
        canonical_text=None,
        resolved_model_id="model-a",
        declared_model="model-a",
        registry=_mini_registry(),
        output_tokens=100,
        calls_per_day=10,
        cache_ctx={"verdict_code": "CACHEABLE", "stable_tokens": 5000,
                   "total_tokens": 5100, "provider_key": "someday-provider",
                   "upstream": None},
        fit_internal=None,
        read_discount_factors=_read_discount_factors(),
    )
    assert result["monthly"]["status"] == "ok"
    assert "cache_savings_usd" not in result["monthly"]


# ---------------------------------------------------------------------------
# analyze_inventory — classify_fn seam + opts
# ---------------------------------------------------------------------------

def _site(**overrides):
    site = {"file": "a.py", "line": 1, "prompt": "hello world",
            "provider": "openai", "model": "model-a"}
    site.update(overrides)
    return site


def test_classify_fn_receives_canonical_and_drives_model_fit():
    calls = []

    def classify_fn(canonical):
        calls.append(canonical)
        return {"benchmark_similarities": _SIMS, "broad_category": "general",
                "subcategory": "chat", "confidence": 0.9}

    doc = analyze_inventory({"sites": [_site()]}, {"run_id": "r"},
                            classify_fn=classify_fn, registry=_mini_registry())
    assert calls == ["user: hello world"]  # the cachelint canonical render
    fit = doc["sites"][0]["checks"]["model_fit"]
    assert fit["status"] in ("ok", "finding")
    assert fit["classification"]["broad_category"] == "general"


def test_classification_seam_bypasses_classify_fn():
    def classify_fn(_canonical):  # pragma: no cover - must not run
        raise AssertionError("classify_fn must not be called for seam sites")

    doc = analyze_inventory(
        {"sites": [_site(_classification={"benchmark_similarities": _SIMS})]},
        {"run_id": "r"}, classify_fn=classify_fn, registry=_mini_registry())
    assert doc["sites"][0]["checks"]["model_fit"]["status"] in ("ok", "finding")


def test_invalid_classifier_result_degrades_honestly():
    doc = analyze_inventory(
        {"sites": [_site()]}, {"run_id": "r"},
        classify_fn=lambda _c: {"nonsense": True}, registry=_mini_registry())
    fit = doc["sites"][0]["checks"]["model_fit"]
    assert fit == {"status": "insufficient_data", "reason": "no classifier available"}


def test_no_classifier_at_all_degrades_honestly():
    doc = analyze_inventory({"sites": [_site()]}, {"run_id": "r"},
                            registry=_mini_registry())
    fit = doc["sites"][0]["checks"]["model_fit"]
    assert fit["status"] == "insufficient_data"


def test_opts_priorities_clamp_like_the_router():
    doc = analyze_inventory({"sites": []},
                            {"run_id": "r", "priorities": {"quality": 99, "cost": 0}},
                            registry=_mini_registry())
    assert doc["interview"]["priorities"] == {"quality": 5, "cost": 1, "speed": 3}


def test_opts_defaults_override_inventory_defaults():
    doc = analyze_inventory(
        {"defaults": {"calls_per_day": 10, "output_tokens": 100},
         "sites": [_site()]},
        {"run_id": "r", "calls_per_day": 99, "output_tokens": 7},
        registry=_mini_registry())
    assert doc["interview"]["defaults"] == {"calls_per_day": 99, "output_tokens": 7}
    cost = doc["sites"][0]["checks"]["cost_exposure"]
    assert cost["monthly"]["calls_per_day"] == 99
    assert cost["output_tokens"] == 7


def test_unknown_check_and_empty_checks_raise():
    with pytest.raises(ValueError, match="unknown check 'bogus'"):
        analyze_inventory({"sites": []}, {"run_id": "r", "checks": ["bogus"]},
                          registry=_mini_registry())
    with pytest.raises(ValueError, match="no checks selected"):
        analyze_inventory({"sites": []}, {"run_id": "r", "checks": []},
                          registry=_mini_registry())


def test_notes_emitted_only_when_present():
    doc = analyze_inventory(
        {"sites": [_site(notes="hot path"), _site(file="b.py")]},
        {"run_id": "r"}, registry=_mini_registry())
    assert doc["sites"][0]["notes"] == "hot path"
    assert "notes" not in doc["sites"][1]


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

def _findings(run_id):
    return {"version": 1, "run_id": run_id, "generated_at": "2026-01-01T00:00:00Z",
            "tool": {"name": "tryaii", "version": "0.0.0"}}


def test_store_roundtrip_and_pointers(tmp_path):
    out = tmp_path / "diagnose"
    inventory = {"sites": [{"file": "a.py", "line": 1, "prompt": "x"}]}

    paths = write_run(out, inventory, _findings("20260101T000000Z"))
    write_run(out, inventory, _findings("20260102T000000Z"))

    assert paths["findings"].is_file() and paths["meta"].is_file()
    # \n newlines on every platform (byte parity with the Node store)
    assert b"\r\n" not in paths["findings"].read_bytes()
    assert json.loads(paths["inventory"].read_text(encoding="utf-8")) == inventory

    assert list_run_ids(out) == ["20260101T000000Z", "20260102T000000Z"]
    assert latest_run_id(out) == "20260102T000000Z"
    assert previous_run_id(out, "20260102T000000Z") == "20260101T000000Z"
    assert previous_run_id(out, "20260101T000000Z") is None
    assert load_run_findings(out, "20260101T000000Z")["run_id"] == "20260101T000000Z"


def test_store_latest_falls_back_to_newest_dir(tmp_path):
    out = tmp_path / "diagnose"
    write_run(out, {}, _findings("20260101T000000Z"))
    (out / "latest").write_text("someday-run\n", encoding="utf-8")  # dangling
    assert latest_run_id(out) == "20260101T000000Z"


def test_store_empty_dir(tmp_path):
    assert list_run_ids(tmp_path / "missing") == []
    assert latest_run_id(tmp_path / "missing") is None
