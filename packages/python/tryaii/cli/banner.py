"""Animated startup banner for the TryAii-DRE CLI.

Renders a blue -> red ``TRYAII`` wordmark to ``stderr`` so ``stdout`` stays clean
for piped / JSON output. Color and animation are suppressed automatically when:

* ``stderr`` is not a TTY (piped, redirected, or running in CI) -- nothing is printed
* ``NO_COLOR`` is set, or ``TERM=dumb`` -- printed once, monochrome, no animation
* ``TRYAII_NO_BANNER`` is set, or ``--no-banner`` is passed -- nothing is printed

A 24-bit gradient is used when the terminal advertises truecolor
(``COLORTERM=truecolor``/``24bit``); otherwise a 256-color two-tone fallback is used.
"""

from __future__ import annotations

import os
import sys
import time

# Big "TRYAII" wordmark (ANSI Shadow style). Every row is the same width so the
# horizontal gradient lines up column-for-column.
_ART = (
    "████████╗██████╗ ██╗   ██╗ █████╗ ██╗██╗",
    "╚══██╔══╝██╔══██╗╚██╗ ██╔╝██╔══██╗██║██║",
    "   ██║   ██████╔╝ ╚████╔╝ ███████║██║██║",
    "   ██║   ██╔══██╗  ╚██╔╝  ██╔══██║██║██║",
    "   ██║   ██║  ██║   ██║   ██║  ██║██║██║",
    "   ╚═╝   ╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝╚═╝╚═╝",
)

# Gradient endpoints: vivid blue -> vivid red.
_BLUE = (56, 132, 255)
_RED = (244, 63, 94)

# 256-color fallback endpoints (DeepSkyBlue / IndianRed).
_BLUE_256 = "\x1b[38;5;39m"
_RED_256 = "\x1b[38;5;203m"

_RESET = "\x1b[0m"
_DIM = "\x1b[2m"

_TITLE = "Diff Routing Engine"
_TAGLINE = "semantic, prompt-aware LLM routing  ·  benchmarks × cost × speed"

_FRAME_DELAY = 0.033  # seconds between revealed wordmark rows


def _enable_windows_ansi() -> bool:
    """Enable VT processing on a Windows console so ANSI escapes render.

    Returns True if VT processing is (now) enabled, False otherwise. No-op and
    True on non-Windows platforms.
    """
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # -12 == STD_ERROR_HANDLE; 0x0004 == ENABLE_VIRTUAL_TERMINAL_PROCESSING.
        handle = kernel32.GetStdHandle(-12)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def _supports_color(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    if not bool(getattr(stream, "isatty", lambda: False)()):
        return False
    return _enable_windows_ansi()


def _truecolor() -> bool:
    return os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")


def _lerp(start: int, end: int, t: float) -> int:
    return round(start + (end - start) * t)


def _gradient(text: str, width: int) -> str:
    """Color each character by its column position across the wordmark."""
    span = max(width - 1, 1)
    out = []
    for i, ch in enumerate(text):
        t = i / span
        r = _lerp(_BLUE[0], _RED[0], t)
        g = _lerp(_BLUE[1], _RED[1], t)
        b = _lerp(_BLUE[2], _RED[2], t)
        out.append(f"\x1b[38;2;{r};{g};{b}m{ch}")
    out.append(_RESET)
    return "".join(out)


def _two_tone(text: str, width: int) -> str:
    """Fallback: blue first ~62%, red remainder (256-color)."""
    split = int(len(text) * 0.62)
    return f"{_BLUE_256}{text[:split]}{_RED_256}{text[split:]}{_RESET}"


def _version() -> str:
    """Best-effort package version without importing the (heavy) package."""
    try:
        from importlib.metadata import version

        return version("tryaii")
    except Exception:
        return ""


def show(stream=None, *, animate: bool = True) -> None:
    """Print the banner to ``stream`` (defaults to ``stderr``).

    Safe to call unconditionally: it self-suppresses for non-interactive
    streams and honors ``NO_COLOR`` / ``TRYAII_NO_BANNER``.
    """
    stream = stream if stream is not None else sys.stderr

    if os.environ.get("TRYAII_NO_BANNER"):
        return
    if not bool(getattr(stream, "isatty", lambda: False)()):
        # Keep redirected / piped / CI output clean.
        return

    color = _supports_color(stream)
    width = max(len(line) for line in _ART)
    colorize = (_gradient if _truecolor() else _two_tone) if color else None
    do_animate = animate and color

    try:
        for line in _ART:
            stream.write((colorize(line, width) if colorize else line) + "\n")
            stream.flush()
            if do_animate:
                time.sleep(_FRAME_DELAY)

        rule = "─" * width
        version = _version()
        ver = f"  ·  v{version}" if version else ""

        rule_line = colorize(rule, width) if colorize else rule
        if color:
            title = f"  {_TITLE}{_DIM}{ver}{_RESET}"
            tagline = f"{_DIM}  {_TAGLINE}{_RESET}"
        else:
            title = f"  {_TITLE}{ver}"
            tagline = f"  {_TAGLINE}"

        stream.write(rule_line + "\n")
        stream.write(title + "\n")
        stream.write(tagline + "\n\n")
        stream.flush()
    except (OSError, UnicodeError):
        # Never let a cosmetic banner break the actual command.
        pass
