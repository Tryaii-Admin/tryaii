"""Shared logging setup for the example scripts."""

from __future__ import annotations

import logging
import os
import sys


def setup_logging(level: int | str | None = None) -> logging.Logger:
    """
    Configure root logging with an informative format.

    Log level can be overridden via the LOG_LEVEL env var (DEBUG, INFO, ...).
    Defaults to DEBUG so tryaii_dre internals are visible.
    """
    if level is None:
        level = os.environ.get("LOG_LEVEL", "DEBUG")

    fmt = "%(asctime)s.%(msecs)03d | %(levelname)-7s | %(name)-38s | %(message)s"
    datefmt = "%H:%M:%S"

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter(fmt=fmt, datefmt=datefmt))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Quiet noisy HTTP libs unless the user explicitly asks for TRACE-level detail.
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.INFO)

    return logging.getLogger("example")
