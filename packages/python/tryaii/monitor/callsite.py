"""Where in the developer's code the monitored call was made.

Thin adapter over cachelint's frame walk (``_introspect.capture_call_site``:
first frame outside the tryaii package, skipping stdlib and site-packages)
that returns the event's ``call_site`` dict. Never raises.
"""

from __future__ import annotations

import os
import re
from typing import Optional

#: A drive letter, a root slash or ``~``: a path that names the machine, not the app.
_ABSOLUTE_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/]|~)")


def is_absolute_file(value) -> bool:
    """True when ``value`` is an absolute or home-relative path (SPEC.md §3.1)."""
    return isinstance(value, str) and bool(_ABSOLUTE_RE.match(value))


def relative_file(path: Optional[str], cwd: Optional[str] = None) -> Optional[str]:
    """The call-site file as the record stores it (TS-226): relative to the
    application's working directory, ``/`` separators; a file outside that
    directory (or on another drive) is its base name only. Never raises."""
    if not path:
        return path
    try:
        base = os.path.abspath(cwd or os.getcwd())
        if is_absolute_file(path):
            full = os.path.abspath(os.path.expanduser(path))
            try:
                rel = os.path.relpath(full, base)
            except ValueError:  # another drive on Windows
                rel = None
            if rel is None or rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel):
                rel = os.path.basename(full)
        else:
            rel = path
        return rel.replace("\\", "/")
    except Exception:  # noqa: BLE001 - a display detail must never break a call
        return os.path.basename(str(path).replace("\\", "/"))


def capture() -> Optional[dict]:
    """``{"file", "line", "function"}`` of the user frame, or None. ``file``
    is relative to the working directory (``relative_file``)."""
    try:
        from tryaii.cachelint._introspect import capture_call_site

        site = capture_call_site()
    except Exception:  # noqa: BLE001 - introspection is best effort
        return None
    if site is None:
        return None
    return {"file": relative_file(site.file), "line": site.line, "function": site.function}
