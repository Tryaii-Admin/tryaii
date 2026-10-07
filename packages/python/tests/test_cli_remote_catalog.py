"""CLI behavior on the catalog the CLI routes on.

`tryaii models` / `tryaii benchmarks` are the user-facing views of the active
catalog -- the packaged starter catalog (45 models / 16 benchmarks) when no
other catalog is selected; these tests pin that both commands surface exactly
that catalog (and that provider filtering works with the OpenRouter-prefix
provider names).
"""

from __future__ import annotations

import json
import sys

import pytest

from tryaii.catalog import starter_bundle
from tryaii.cli import main as cli_main
from tryaii.scoring.benchmarks import NORMALIZATION_RANGES

STARTER_MODELS = starter_bundle().counts["models"]
STARTER_BENCHMARKS = starter_bundle().counts["benchmarks"]


@pytest.fixture(autouse=True)
def _no_banner(monkeypatch):
    monkeypatch.setenv("TRYAII_NO_BANNER", "1")


def _run(monkeypatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["tryaii", *argv])
    cli_main.cli()


def test_models_json_lists_the_starter_catalog(monkeypatch, capsys):
    _run(monkeypatch, "models", "--json")
    data = json.loads(capsys.readouterr().out)
    assert len(data) == STARTER_MODELS == 45
    ids = {m["model_id"] for m in data}
    assert "openai/gpt-4o" in ids
    assert "anthropic/claude-opus-4.5" in ids
    assert all("/" in i for i in ids)


def test_models_provider_filter_uses_openrouter_prefixes(monkeypatch, capsys):
    _run(monkeypatch, "models", "--provider", "anthropic", "--json")
    data = json.loads(capsys.readouterr().out)
    assert data, "expected anthropic models"
    assert all(m["provider"] == "anthropic" for m in data)


def test_models_provider_filter_is_case_insensitive(monkeypatch, capsys):
    _run(monkeypatch, "models", "--provider", "Anthropic", "--json")
    data = json.loads(capsys.readouterr().out)
    assert data and all(m["provider"] == "anthropic" for m in data)


def test_models_text_output_reports_the_starter_count(monkeypatch, capsys):
    _run(monkeypatch, "models")
    out = capsys.readouterr().out
    assert f"Available Models ({STARTER_MODELS})" in out


def test_benchmarks_json_lists_the_starter_benchmarks_with_ranges(monkeypatch, capsys):
    _run(monkeypatch, "benchmarks", "--json")
    data = json.loads(capsys.readouterr().out)
    assert len(data) == STARTER_BENCHMARKS == 16
    assert [b["name"] for b in data] == starter_bundle().benchmark_names
    by_name = {b["name"]: b for b in data}
    assert set(by_name) == set(NORMALIZATION_RANGES)
    # Spot-check that the ELO and percentage scales made it through. The
    # ranges are catalog-derived (p25..max), so the scale -- not a hard-coded
    # pair -- is what is pinned; the exact numbers live in the bundle's
    # normalization_ranges.json and are compared there.
    arena = by_name["Chatbot Arena Elo"]["normalization"]
    assert 1000 < arena["min_score"] < arena["max_score"] < 1600
    assert (arena["min_score"], arena["max_score"]) == (
        NORMALIZATION_RANGES["Chatbot Arena Elo"].min_score,
        NORMALIZATION_RANGES["Chatbot Arena Elo"].max_score,
    )
    gpqa = by_name["GPQA"]["normalization"]
    assert 0.0 < gpqa["min_score"] < gpqa["max_score"] <= 100.0
    assert (gpqa["min_score"], gpqa["max_score"]) == (
        NORMALIZATION_RANGES["GPQA"].min_score,
        NORMALIZATION_RANGES["GPQA"].max_score,
    )


def test_benchmarks_text_output_mentions_remote_benchmarks(monkeypatch, capsys):
    _run(monkeypatch, "benchmarks")
    out = capsys.readouterr().out
    for name in ("GPQA", "Terminal-bench-Hard", "LiveCodeBench", "Chatbot Arena Elo"):
        assert name in out
