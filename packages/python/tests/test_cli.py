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


@pytest.mark.parametrize("argv", [(), ("help",), ("--help",), ("-h",)])
def test_global_help_prints_shared_text_from_any_position(monkeypatch, capsys, argv):
    _run(monkeypatch, *argv)
    assert capsys.readouterr().out == cli_main.HELP


@pytest.mark.parametrize(
    "argv,command",
    [
        (("help", "route"), "route"),
        (("help", "eval"), "eval"),
        (("eval", "--help"), "eval"),
        (("route", "-h"), "route"),
        (("models", "--help"), "models"),
    ],
)
def test_command_help_prints_per_command_text(monkeypatch, capsys, argv, command):
    _run(monkeypatch, *argv)
    assert capsys.readouterr().out == cli_main.COMMAND_HELP[command]


@pytest.mark.parametrize(
    "argv",
    [("help", "help"), ("help", "--help"), ("help", "-h")],
)
def test_help_command_documents_itself(monkeypatch, capsys, argv):
    # The new help command is self-documenting both ways, like every other
    # command: the subcommand form (`tryaii help help`) and the flag form
    # (`tryaii help --help` / `tryaii help -h`) both print the help page.
    _run(monkeypatch, *argv)
    assert capsys.readouterr().out == cli_main.COMMAND_HELP["help"]


def test_bare_help_still_prints_global_overview(monkeypatch, capsys):
    # With no topic and no help flag, `tryaii help` stays the global overview.
    _run(monkeypatch, "help")
    assert capsys.readouterr().out == cli_main.HELP


def test_unknown_help_topic_is_usage_error(monkeypatch, capsys):
    with pytest.raises(SystemExit) as excinfo:
        _run(monkeypatch, "help", "frobnicate")
    assert excinfo.value.code == 2
    assert "unknown help topic" in capsys.readouterr().err


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
