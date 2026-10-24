"""The active OpenTelemetry span at call time (SPEC.md "Langfuse export").

The wrapper records ``{"trace_id", "span_id"}`` of the span that is active
when ``create`` is called (for example a Langfuse ``@observe`` function or
any other OpenTelemetry instrumentation), so the Langfuse exporter can put
the TryAii span inside that trace. ``opentelemetry`` is never a dependency:
when it cannot be imported, nothing is recorded. Never raises.
"""

from __future__ import annotations

from typing import Optional

_UNSET = object()
_trace = _UNSET


def _trace_api():
    global _trace
    if _trace is _UNSET:
        try:
            from opentelemetry import trace  # type: ignore[import-not-found]
        except Exception:  # noqa: BLE001 - not installed (or broken): no capture
            trace = None
        _trace = trace
    return _trace


def active_span_ids() -> Optional[dict]:
    """``{"trace_id": <32 hex>, "span_id": <16 hex>}`` of the active span,
    or None (no OpenTelemetry API, no active span, or an invalid one)."""
    try:
        trace = _trace_api()
        if trace is None:
            return None
        ctx = trace.get_current_span().get_span_context()
        if not ctx.is_valid:
            return None
        return {"trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x")}
    except Exception:  # noqa: BLE001 - fail-open
        return None


# --- the TryAii parent for Langfuse's OpenAI integration ----------------------

ORIGIN_TRYAII = "tryaii"


def langfuse_wrapped(fn) -> bool:
    """True when ``fn`` (the class ``create`` the wrapper is about to call)
    is wrapped by Langfuse's OpenAI integration: ``import langfuse.openai``
    patches the class with a wrapt wrapper defined in ``langfuse.openai``.
    Read without importing langfuse. Never raises."""
    try:
        for _ in range(4):  # a few wrapt layers at most
            wrapper = getattr(fn, "_self_wrapper", None)
            if wrapper is None:
                return False
            if str(getattr(wrapper, "__module__", "") or "").startswith("langfuse"):
                return True
            fn = getattr(fn, "__wrapped__", None)
        return False
    except Exception:  # noqa: BLE001 - fail-open
        return False


def attach_parent(trace_id: str, span_id: str):
    """Make a non-recording span with these ids the active span (so an
    integration that starts its span inside the call becomes its child);
    the token for :func:`detach`, or None when OpenTelemetry is missing or
    anything fails."""
    try:
        trace = _trace_api()
        if trace is None:
            return None
        from opentelemetry import context  # type: ignore[import-not-found]

        span_context = trace.SpanContext(trace_id=int(trace_id, 16), span_id=int(span_id, 16),
                                         is_remote=True,
                                         trace_flags=trace.TraceFlags(trace.TraceFlags.SAMPLED))
        return context.attach(trace.set_span_in_context(trace.NonRecordingSpan(span_context)))
    except Exception:  # noqa: BLE001 - fail-open
        return None


def detach(token) -> None:
    if token is None:
        return
    try:
        from opentelemetry import context  # type: ignore[import-not-found]

        context.detach(token)
    except Exception:  # noqa: BLE001 - fail-open
        pass
