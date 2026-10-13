"""Where in the developer's code the monitored call was made.

Thin adapter over cachelint's frame walk (``_introspect.capture_call_site``:
first frame outside the tryaii package, skipping stdlib and site-packages)
that returns the event's ``call_site`` dict. Never raises.
"""

from __future__ import annotations

from typing import Optional


def capture() -> Optional[dict]:
    """``{"file", "line", "function"}`` of the user frame, or None."""
    try:
        from tryaii.cachelint._introspect import capture_call_site

        site = capture_call_site()
    except Exception:  # noqa: BLE001 - introspection is best effort
        return None
    if site is None:
        return None
    return {"file": site.file, "line": site.line, "function": site.function}
