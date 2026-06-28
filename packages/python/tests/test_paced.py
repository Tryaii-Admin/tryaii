"""Unit tests for the paced output helper (mirrors the Node output.test.ts).

Exercise both branches -- instant dump vs. line-by-line reveal -- with a fake
stdout and time.sleep stubbed out, so the pacing logic is covered without real
delays or a real TTY.
"""

from __future__ import annotations

import sys

import pytest

from tryaii.cli.main import _write_paced

TEXT = "line one\nline two\nline three"


class FakeStdout:
    def __init__(self, isatty: bool):
        self._isatty = isatty
        self.chunks: list[str] = []

    def write(self, s: str) -> None:
        self.chunks.append(s)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return self._isatty

    @property
    def text(self) -> str:
        return "".join(self.chunks)


@pytest.fixture(autouse=True)
def _no_sleep_no_banner(monkeypatch):
    # Never actually sleep, and start from a clean (banner-enabled) env.
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    monkeypatch.delenv("TRYAII_NO_BANNER", raising=False)


def test_dumps_in_one_write_when_not_tty(monkeypatch):
    fake = FakeStdout(isatty=False)
    monkeypatch.setattr(sys, "stdout", fake)
    _write_paced(TEXT)
    assert len(fake.chunks) == 1
    assert fake.text == TEXT


def test_reveals_line_by_line_on_tty(monkeypatch):
    fake = FakeStdout(isatty=True)
    monkeypatch.setattr(sys, "stdout", fake)
    _write_paced(TEXT)
    # one write per line, reassembled output byte-identical to the input
    assert len(fake.chunks) == 3
    assert fake.text == TEXT
    assert fake.chunks == ["line one\n", "line two\n", "line three"]


def test_dumps_instantly_on_tty_when_no_banner(monkeypatch):
    monkeypatch.setenv("TRYAII_NO_BANNER", "1")
    fake = FakeStdout(isatty=True)
    monkeypatch.setattr(sys, "stdout", fake)
    _write_paced(TEXT)
    assert len(fake.chunks) == 1
    assert fake.text == TEXT


def test_single_line_has_no_trailing_newline(monkeypatch):
    fake = FakeStdout(isatty=True)
    monkeypatch.setattr(sys, "stdout", fake)
    _write_paced("just one line")
    assert fake.chunks == ["just one line"]
