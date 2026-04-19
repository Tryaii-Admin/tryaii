"""Tiny .env loader -- no external dependency."""

from __future__ import annotations

import os
from pathlib import Path


def load_env(path: str | Path = None) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ (without overriding)."""
    env_path = Path(path) if path else Path(__file__).with_name(".env")
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
