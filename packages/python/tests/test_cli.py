"""CLI surface behavior tests.

These pin the global-flag behavior that is kept in parity with the Node CLI:
--version/-V, help in any position (including bare `tryaii help`), usage
errors exiting 2, and runtime errors printing a clean one-line message.
"""

from __future__ import annotations

import sys

import pytest

from tryaii import __version__
from tryaii.cli import main as cli_main


@pytest.fixture(autouse=True)
def _no_banner(monkeypatch):
    monkeypatch.setenv("TRYAII_NO_BANNER", "1")


def _run(monkeypatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["tryaii", *argv])
    cli_main.cli()


def test_version_flag(monkeypatch, capsys):
    _run(monkeypatch, "--version")
    assert capsys.readouterr().out.strip() == __version__


def test_version_short_flag(monkeypatch, capsys):
    _run(monkeypatch, "-V")
    assert capsys.readouterr().out.strip() == __version__


@pytest.mark.parametrize(
    "argv",
    [(), ("help",), ("--help",), ("-h",), ("eval", "--help"), ("route", "-h")],
)
def test_help_prints_shared_text_from_any_position(monkeypatch, capsys, argv):
    _run(monkeypatch, *argv)
    assert capsys.readouterr().out == cli_main.HELP


def test_unknown_command_is_usage_error(monkeypatch):
    with pytest.raises(SystemExit) as excinfo:
        _run(monkeypatch, "frobnicate")
    assert excinfo.value.code == 2


def test_missing_route_prompt_is_usage_error(monkeypatch):
    with pytest.raises(SystemExit) as excinfo:
        _run(monkeypatch, "route")
    assert excinfo.value.code == 2


def test_invalid_priority_value_is_usage_error(monkeypatch):
    with pytest.raises(SystemExit) as excinfo:
        _run(monkeypatch, "route", "hi", "--quality", "abc")
    assert excinfo.value.code == 2


def test_negative_difficulty_gamma_is_usage_error(monkeypatch, capsys, tmp_path):
    prompts = tmp_path / "prompts.json"
    prompts.write_text('["hi"]', encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        _run(monkeypatch, "eval", str(prompts), "--difficulty-gamma=-1")
    assert excinfo.value.code == 2
    assert "difficulty-gamma" in capsys.readouterr().err


def test_runtime_errors_print_clean_message(monkeypatch, capsys, tmp_path):
    bad = tmp_path / "not-an-array.json"
    bad.write_text('{"prompt": "hi"}', encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        _run(monkeypatch, "eval", str(bad))
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "Traceback" not in err
