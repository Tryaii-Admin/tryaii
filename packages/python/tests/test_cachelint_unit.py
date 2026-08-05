"""Unit tests for cachelint internals the golden fixtures cannot reach.

Fixtures pin end-to-end behavior over real text; these tests cover branch
logic that no text corpus can produce (identity-based next_stable with
co-located blockers), boundary tables (section attribution, the ISO-8601
grammar), and the canonical-JSON helpers whose byte output the TS port
mirrors (SPEC.md §1.3-§1.6).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import pytest

from tryaii.cachelint._iso8601 import parse_ts_ms
from tryaii.cachelint._jsonutil import normalize_numbers, sorted_minified_dumps
from tryaii.cachelint.analyzer import _next_blocking_start, _section_of, build_canonical
from tryaii.cachelint.detectors import Finding


def _finding(start: int, end: int, kind: str = "uuid") -> Finding:
    return Finding(kind=kind, severity="high", start=start, end=end,
                   excerpt="", why="")


class TestNextBlockingStart:
    """SPEC.md delta g: exclusion by identity, not by start offset."""

    def test_colocated_second_blocker_counts(self):
        first = _finding(10, 20, "session-id")
        twin = _finding(10, 20, "secret-like")   # same span, different detector
        later = _finding(50, 60)
        assert _next_blocking_start([first, twin, later], first) == 10

    def test_single_blocker_returns_none(self):
        first = _finding(10, 20)
        assert _next_blocking_start([first], first) is None

    def test_ordinary_case(self):
        first = _finding(10, 20)
        later = _finding(50, 60)
        assert _next_blocking_start([first, later], first) == 50


class TestSectionOf:
    """SPEC.md delta l: separator / past-end offsets map to the PRECEDING section."""

    @pytest.fixture()
    def sections(self):
        # tools(0..9) \n\n system(11..14) \n\n messages[0](16..23)
        _, sections, _ = build_canonical({
            "tools": [{"a": 1}],
            "system": "SYS",
            "messages": [{"role": "user", "content": "q"}],
        })
        return sections

    def test_inside_each_section(self, sections):
        assert _section_of(sections, 0).name == "tools"
        assert _section_of(sections, sections[1].start).name == "system"
        assert _section_of(sections, sections[2].end - 1).name == "messages[0]:user"

    def test_separator_maps_to_preceding(self, sections):
        # offsets at tools.end and inside the \n\n separator belong to tools
        assert _section_of(sections, sections[0].end).name == "tools"
        assert _section_of(sections, sections[0].end + 1).name == "tools"

    def test_past_end_maps_to_last(self, sections):
        assert _section_of(sections, sections[2].end + 5).name == "messages[0]:user"

    def test_empty_sections(self):
        assert _section_of([], 3) is None


class TestIso8601:
    """SPEC.md §1.6: the strict shared grammar, identical in both SDKs."""

    def test_epoch_zero(self):
        assert parse_ts_ms("1970-01-01T00:00:00Z") == 0

    @pytest.mark.parametrize("value", [
        "2026-07-30T09:00:00Z",
        "2026-07-30t09:00:00z",
        "2026-07-30 09:00:00+00:00",
        "2026-07-30T11:00:00+02:00",
        "2026-07-30T11:34:00+0234",
        "2026-07-30T09:00:00.25Z",
        "2026-07-30T09:00",
        "2026-07-30",
    ])
    def test_accepts(self, value):
        assert parse_ts_ms(value) is not None

    def test_matches_datetime_for_aware_values(self):
        expect = int(datetime(2026, 7, 30, 9, 0, tzinfo=timezone.utc).timestamp() * 1000)
        assert parse_ts_ms("2026-07-30T09:00:00Z") == expect
        assert parse_ts_ms("2026-07-30T11:00:00+02:00") == expect
        assert parse_ts_ms("2026-07-30T09:00:00") == expect          # naive = UTC

    def test_fraction_truncates_to_ms(self):
        base = parse_ts_ms("2026-07-30T09:00:00Z")
        assert parse_ts_ms("2026-07-30T09:00:00.123456789Z") == base + 123
        assert parse_ts_ms("2026-07-30T09:00:00.5Z") == base + 500

    def test_date_only_is_midnight(self):
        assert parse_ts_ms("2026-07-30") == parse_ts_ms("2026-07-30T00:00:00Z")

    def test_leap_day(self):
        assert parse_ts_ms("2024-02-29") is not None
        assert parse_ts_ms("2026-02-29") is None

    @pytest.mark.parametrize("value", [
        None, "", "yesterday-ish", "2026-13-01", "2026-00-10", "2026-01-32",
        "2026-07-30T24:00", "2026-07-30T09:60", "2026-07-30T09:00:61",
        "2026-7-30", "20260730T090000Z", "2026-07-30T09:00:00+25:00",
        "2026-07-30T09:00:00Z extra", "2026-07-30Z09:00",
    ])
    def test_rejects(self, value):
        assert parse_ts_ms(value) is None


class TestJsonUtil:
    """SPEC.md §1.3/§1.4: the byte-parity JSON helpers."""

    def test_integral_floats_become_ints(self):
        assert normalize_numbers({"a": 1.0, "b": [2.5, 100.0], "c": True}) == \
            {"a": 1, "b": [2.5, 100], "c": True}
        assert isinstance(normalize_numbers(1.0), int)
        assert isinstance(normalize_numbers(True), bool)

    def test_sorted_minified(self):
        assert sorted_minified_dumps([{"zeta": 1.0, "alpha": {"b": 2.5, "a": [1.0, "x", 3]}}]) \
            == '[{"alpha":{"a":[1,"x",3],"b":2.5},"zeta":1}]'

    def test_non_ascii_verbatim(self):
        assert sorted_minified_dumps({"label": "café"}) == '{"label":"café"}'


class TestTiktokenRequired:
    """SPEC.md delta o: a missing tiktoken raises with install instructions."""

    def test_import_error_message(self, monkeypatch):
        from tryaii.cachelint import tokenizers

        monkeypatch.setattr(tokenizers, "_TIKTOKEN_ENC", None)
        monkeypatch.setitem(sys.modules, "tiktoken", None)   # import -> ImportError
        with pytest.raises(ImportError) as excinfo:
            tokenizers.count_tokens("hello", "openai")
        assert "pip install tryaii[cachelint]" in str(excinfo.value)

    def test_heuristic_paths_never_need_tiktoken(self, monkeypatch):
        from tryaii.cachelint import tokenizers

        monkeypatch.setattr(tokenizers, "_TIKTOKEN_ENC", None)
        monkeypatch.setitem(sys.modules, "tiktoken", None)
        assert tokenizers.count_tokens("hello world", "anthropic").tokens > 0
