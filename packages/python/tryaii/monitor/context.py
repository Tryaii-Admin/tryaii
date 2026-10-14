"""The ``process(name)`` context: which sub-process the current code runs in.

A :class:`contextvars.ContextVar`, so the name follows ``asyncio`` tasks
created inside the block; threads started inside it start empty (document
``tryaii_process=`` or ``wrap(process=)`` for those).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Optional

_process_var: ContextVar[Optional[str]] = ContextVar("tryaii_monitor_process", default=None)


def current_process() -> Optional[str]:
    """The sub-process name set by the innermost :func:`process` block."""
    return _process_var.get()


@contextmanager
def process(name: str) -> Iterator[None]:
    """Name the sub-process for every monitored call made inside the block.

    Per-call ``tryaii_process=`` still wins (SPEC.md §2.6).
    """
    token = _process_var.set(name)
    try:
        yield
    finally:
        _process_var.reset(token)
