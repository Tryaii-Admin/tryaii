"""``tryaii.monitor.wrap`` / ``install`` for the ``anthropic`` SDK (TS-169).

The Anthropic counterpart of :mod:`tryaii.monitor.wrapper_openai`, built on
the same :class:`~tryaii.monitor.wrapper_openai.Monitor`. It replaces
``client.messages.create`` and ``client.messages.stream`` on the instance
(duck-typed: ``anthropic`` is never imported here, except by
:func:`install`, which patches the SDK's ``Messages`` / ``AsyncMessages``
classes once ``anthropic`` has been imported). The same classes serve
``Anthropic``, ``AnthropicBedrock`` and ``AnthropicVertex`` (and their async
twins), so all of them are covered.

Per call, like the openai wrapper: call site and sub-process are captured
synchronously, the mode is resolved, the provider is called OUTSIDE monitor
code (its exceptions propagate unchanged after an error event), and usage,
latency and the plan are handed to the transport worker.

Differences from the openai wrapper:

* Routing text: Anthropic's top-level ``system`` (a string or text blocks)
  is read as the system message, then the last ``user`` message (SPEC.md
  §2.1 applied to the Messages API shape).
* The event's ``provider`` is ``anthropic`` (Bedrock and Vertex clients
  too: they answer with the same Messages-API usage). It is not a SPEC.md
  §2.2 class; routing treats it like ``other``: a Messages-API client is
  monitored and shadow-routed, but its model is never swapped (``stable``,
  ``dynamic`` and ``managed`` record their decision; the request is sent as
  written, and a managed model other than the one written is recorded as
  ``unreachable_managed``). The label tells the server that
  ``prompt_tokens`` excludes the cache (its hit rate adds the cached and
  written tokens back, §3.1b).
* Usage (SPEC.md §3.1b, ``anthropic`` row): ``prompt_tokens`` =
  ``input_tokens`` as reported (it EXCLUDES cached and written tokens),
  ``completion_tokens`` = ``output_tokens``, ``total_tokens`` = their sum
  when both are reported; the cache columns come from
  :func:`~tryaii.monitor.events.normalize_cache_usage` (``"anthropic"``):
  ``cache_read_input_tokens`` → ``cached_tokens``,
  ``cache_creation_input_tokens`` → ``cache_write_tokens``,
  ``cache_creation.ephemeral_5m_input_tokens`` / ``ephemeral_1h_input_tokens``
  → ``cache_writes`` (``"5m"`` / ``"1h"``). Nothing is derived that the
  response did not report.
* Streams (``stream=True`` and ``messages.stream()``) are recorded from the
  ``message_start`` usage merged with the ``message_delta`` usage.
* Bedrock model ids (``us.anthropic.claude-...-v1:0``) are resolved to the
  catalog anchor after stripping the region / vendor prefix and the version
  suffix; Vertex ids (``claude-...@20250929``) resolve as they are.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from typing import Any, Callable, Optional

from tryaii.monitor import wrapper_openai as _wo
from tryaii.monitor.events import _count, _get, normalize_cache_usage
from tryaii.monitor.winner import PROVIDER_ANTHROPIC, resolve_anchor, route_allowed
from tryaii.monitor.wrapper_openai import (
    PER_CALL_PROCESS_KW,
    AsyncMonitoredStream,
    CallPlan,
    Monitor,
    MonitoredStream,
    Transport,
    _elapsed_ms,
    _is_coroutine_function,
)

logger = logging.getLogger("tryaii.monitor")

#: The ``normalize_cache_usage`` shape of the Messages API.
CACHE_SHAPE = "anthropic"
#: Cache lifetimes a request's ``cache_control.ttl`` can ask for, shortest first.
_TTL_ORDER = ("5m", "1h")
#: Bedrock cross-region / vendor prefix and version suffix of a model id.
_BEDROCK_PREFIX = re.compile(r"^(?:[a-z]{2,6}(?:-[a-z]+)?\.)?anthropic\.")
_BEDROCK_SUFFIX = re.compile(r"-v\d+(?::\d+)?$")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _dump(obj: Any) -> Any:
    """A pydantic object → dict (``model_dump``); anything else unchanged."""
    if obj is None or isinstance(obj, dict):
        return obj
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            return dump()
        except Exception:  # noqa: BLE001
            return obj
    return obj


def usage_from_anthropic(raw_usage: Any) -> Optional[dict]:
    """The event's ``usage`` (contract keys) from a Messages-API ``usage``
    (object or dict); None when there is none. ``input_tokens`` is recorded
    unchanged as ``prompt_tokens`` (SPEC.md §3.1b rule 2)."""
    raw = _dump(raw_usage)
    if raw is None:
        return None
    prompt = _count(_get(raw, "input_tokens"))
    completion = _count(_get(raw, "output_tokens"))
    total = prompt + completion if prompt is not None and completion is not None else None
    out = {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}
    out.update(normalize_cache_usage(CACHE_SHAPE, raw))
    # API.md v1.7: the Messages API reports no reasoning share of output_tokens.
    out["reasoning_tokens"] = None
    return out


def merge_stream_usage(current: Optional[dict], update: Any) -> Optional[dict]:
    """Fold one stream event's usage into the running raw usage: the
    ``message_start`` usage first, then each ``message_delta`` usage, whose
    non-null fields (cumulative counts) replace the earlier ones."""
    data = _dump(update)
    if not isinstance(data, dict):
        return current
    merged = dict(current or {})
    for key, value in data.items():
        if value is not None:
            merged[key] = value
    return merged


def routing_messages(kwargs: dict) -> Any:
    """The request's ``messages`` with its top-level ``system`` (a string or
    a list of text blocks) put first as a ``system`` message, so
    :func:`~tryaii.monitor.extract.routing_text` and the cache lint read the
    Messages API like a chat-completions request."""
    messages = kwargs.get("messages")
    system = kwargs.get("system")
    if not isinstance(messages, list):
        messages = list(messages) if isinstance(messages, tuple) else messages
    if system is None or not isinstance(system, (str, list, tuple)):
        return messages
    if isinstance(system, tuple):
        system = list(system)
    if not isinstance(messages, list):
        return [{"role": "system", "content": system}]
    return [{"role": "system", "content": system}, *messages]


def _ttl_of(block: Any) -> Optional[str]:
    control = _get(block, "cache_control")
    ttl = _get(control, "ttl")
    if isinstance(ttl, str) and ttl.strip().lower() in _TTL_ORDER:
        return ttl.strip().lower()
    return None


def cache_ttl_policy(kwargs: dict) -> Optional[str]:
    """The longest explicit ``cache_control.ttl`` the request asks for (top
    level, ``tools``, ``system`` blocks, message content blocks), or None
    when it names none (SPEC.md §3.1b ``cache_ttl_policy``)."""
    found = []

    def visit(block: Any) -> None:
        ttl = _ttl_of(block)
        if ttl is not None:
            found.append(ttl)

    visit(kwargs)
    for key in ("tools", "system"):
        value = kwargs.get(key)
        if isinstance(value, (list, tuple)):
            for block in value:
                visit(block)
    messages = kwargs.get("messages")
    if isinstance(messages, (list, tuple)):
        for msg in messages:
            visit(msg)
            content = _get(msg, "content")
            if isinstance(content, (list, tuple)):
                for block in content:
                    visit(block)
    if not found:
        return None
    return max(found, key=_TTL_ORDER.index)


def anchor_model_id(model: str) -> str:
    """A Bedrock id without its region / vendor prefix and version suffix
    (``us.anthropic.claude-haiku-4-5-20251001-v1:0`` →
    ``claude-haiku-4-5-20251001``); other ids unchanged."""
    text = model.strip()
    stripped = _BEDROCK_PREFIX.sub("", text)
    if stripped != text:
        stripped = _BEDROCK_SUFFIX.sub("", stripped)
    return stripped


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

class AnthropicMonitor(Monitor):
    """:class:`Monitor` for a Messages-API client (event provider
    ``anthropic``, routed like class ``other``)."""

    def __init__(self, *, app: Optional[str] = None, process: Optional[str] = None,
                 mode: Optional[str] = None, transport: Optional[Transport] = None,
                 cachelint: bool = False, vectors: Optional[bool] = None):
        super().__init__(PROVIDER_ANTHROPIC, app=app, process=process, mode=mode,
                         transport=transport, cachelint=cachelint, vectors=vectors)

    def before(self, kwargs: dict, per_call_process: Any, stream: bool) -> Optional[CallPlan]:
        try:
            view = {"model": kwargs.get("model"), "messages": routing_messages(kwargs)}
        except Exception:  # noqa: BLE001 - fail-open
            logger.debug("monitor: anthropic before() failed", exc_info=True)
            return None
        plan = super().before(view, per_call_process, stream)
        if plan is None:
            return None
        try:
            if plan.anchor is None and plan.model_requested:
                alt = anchor_model_id(plan.model_requested)
                if alt != plan.model_requested:
                    registry = self.transport.registry()
                    anchor = resolve_anchor(alt, registry)
                    if anchor is not None:
                        plan.anchor = anchor
                        plan.route_allowed = route_allowed(self.provider, registry.model_ids,
                                                           anchor, plan.policy)
            # The base read the chat-shaped view; Messages-API breakpoints
            # sit on tools, system and content blocks.
            plan.cache_ttl_policy = cache_ttl_policy(kwargs)
        except Exception:  # noqa: BLE001
            logger.debug("monitor: anthropic before() failed", exc_info=True)
        return plan

    def apply(self, plan: CallPlan, kwargs: dict) -> None:
        """The base step on a chat-shaped view of the request (``system`` as
        the first message) for the cache lint; a model rewrite (never chosen:
        only the anchor is reachable) is copied back."""
        try:
            view = {"model": kwargs.get("model"), "messages": routing_messages(kwargs)}
            tools = kwargs.get("tools")
            if isinstance(tools, list):
                view["tools"] = tools
        except Exception:  # noqa: BLE001
            logger.debug("monitor: anthropic apply() failed", exc_info=True)
            return
        super().apply(plan, view)
        if view.get("model") != kwargs.get("model") and "model" in kwargs:
            kwargs["model"] = view["model"]

    def usage_shape(self) -> Any:
        """Messages-API usage: the ``anthropic`` shape (SPEC.md §3.1b)."""
        return CACHE_SHAPE

    def finish(self, plan: CallPlan, *, usage: Any, latency_ms: Optional[int],
               error: Optional[BaseException]) -> None:
        try:
            mapped = usage_from_anthropic(usage)
        except Exception:  # noqa: BLE001
            logger.debug("monitor: anthropic usage failed", exc_info=True)
            mapped = None
        super().finish(plan, usage=mapped, latency_ms=latency_ms, error=error)


# ---------------------------------------------------------------------------
# Streams
# ---------------------------------------------------------------------------

def _note_event(stream: Any, event: Any) -> Any:
    kind = getattr(event, "type", None)
    if kind is None and isinstance(event, dict):
        kind = event.get("type")
    if kind == "message_start":
        message = _get(event, "message")
        stream._usage = merge_stream_usage(None, _get(message, "usage"))
    elif kind == "message_delta":
        stream._usage = merge_stream_usage(stream._usage, _get(event, "usage"))
    return event


class AnthropicStream(MonitoredStream):
    """A ``stream=True`` response: raw events forwarded unchanged; usage
    from ``message_start`` merged with ``message_delta``."""

    def _note(self, chunk: Any) -> Any:
        try:
            return _note_event(self, chunk)
        except Exception:  # noqa: BLE001
            return chunk


class AsyncAnthropicStream(AsyncMonitoredStream):
    def _note(self, chunk: Any) -> Any:
        try:
            return _note_event(self, chunk)
        except Exception:  # noqa: BLE001
            return chunk


def _snapshot_usage(stream: Any) -> Any:
    """The accumulated message usage of a ``MessageStream`` (None before the
    first event)."""
    try:
        snapshot = stream.current_message_snapshot
    except Exception:  # noqa: BLE001 - no event received yet
        return None
    return getattr(snapshot, "usage", None)


class _StreamManager:
    """Wraps ``messages.stream()``'s ``MessageStreamManager``: the request is
    made on ``__enter__``; the call is recorded on ``__exit__`` from the
    stream's accumulated message."""

    def __init__(self, monitor: AnthropicMonitor, plan: CallPlan, inner: Any, t0: float):
        self._monitor = monitor
        self._plan = plan
        self._inner = inner
        self._t0 = t0
        self._stream: Any = None
        self._done = False

    def _record(self, exc: Optional[BaseException]) -> None:
        if self._done:
            return
        self._done = True
        usage = _snapshot_usage(self._stream) if self._stream is not None else None
        self._monitor.finish(self._plan, usage=usage, latency_ms=_elapsed_ms(self._t0),
                             error=exc)

    def __enter__(self):
        try:
            self._stream = self._inner.__enter__()
        except Exception as exc:
            self._record(exc)
            raise
        return self._stream

    def __exit__(self, exc_type, exc, tb):
        self._record(exc)
        return self._inner.__exit__(exc_type, exc, tb)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _AsyncStreamManager(_StreamManager):
    """The async manager: ``AsyncMessages.stream`` is a plain ``def`` and
    the request starts on ``__aenter__``, so the decision (which never
    changes the request of a Messages-API client) is taken there, off
    the event loop."""

    async def __aenter__(self):
        if self._monitor.needs_decision(self._plan):
            import asyncio

            await asyncio.get_running_loop().run_in_executor(None, self._monitor.decide,
                                                             self._plan)
        self._t0 = time.perf_counter()
        try:
            self._stream = await self._inner.__aenter__()
        except Exception as exc:
            self._record(exc)
            raise
        return self._stream

    async def __aexit__(self, exc_type, exc, tb):
        self._record(exc)
        return await self._inner.__aexit__(exc_type, exc, tb)


# ---------------------------------------------------------------------------
# The create() / stream() wrappers
# ---------------------------------------------------------------------------

def _run_sync(monitor: AnthropicMonitor, original: Callable, plan: Optional[CallPlan],
              args: tuple, kwargs: dict) -> Any:
    if plan is not None:
        monitor.decide(plan)
        monitor.apply(plan, kwargs)
    t0 = time.perf_counter()
    try:
        response = original(*args, **kwargs)
    except Exception as exc:
        if plan is not None:
            monitor.finish(plan, usage=None, latency_ms=_elapsed_ms(t0), error=exc)
        raise
    if plan is None:
        return response
    if plan.stream:
        return AnthropicStream(
            response,
            lambda usage, exc: monitor.finish(plan, usage=usage, latency_ms=_elapsed_ms(t0),
                                              error=exc))
    monitor.finish(plan, usage=getattr(response, "usage", None),
                   latency_ms=_elapsed_ms(t0), error=None)
    return response


async def _run_async(monitor: AnthropicMonitor, original: Callable, plan: Optional[CallPlan],
                     args: tuple, kwargs: dict) -> Any:
    if plan is not None:
        if monitor.needs_decision(plan):
            import asyncio

            await asyncio.get_running_loop().run_in_executor(None, monitor.decide, plan)
        monitor.apply(plan, kwargs)
    t0 = time.perf_counter()
    try:
        response = await original(*args, **kwargs)
    except Exception as exc:
        if plan is not None:
            monitor.finish(plan, usage=None, latency_ms=_elapsed_ms(t0), error=exc)
        raise
    if plan is None:
        return response
    if plan.stream:
        return AsyncAnthropicStream(
            response,
            lambda usage, exc: monitor.finish(plan, usage=usage, latency_ms=_elapsed_ms(t0),
                                              error=exc))
    monitor.finish(plan, usage=getattr(response, "usage", None),
                   latency_ms=_elapsed_ms(t0), error=None)
    return response


def _call_create(monitor: AnthropicMonitor, target: Callable, is_async: bool,
                 args: tuple, kwargs: dict) -> Any:
    per_call = kwargs.pop(PER_CALL_PROCESS_KW, None)
    plan = monitor.before(kwargs, per_call, bool(kwargs.get("stream")))
    if is_async:
        return _run_async(monitor, target, plan, args, kwargs)
    return _run_sync(monitor, target, plan, args, kwargs)


def _call_stream(monitor: AnthropicMonitor, target: Callable, is_async: bool,
                 args: tuple, kwargs: dict) -> Any:
    per_call = kwargs.pop(PER_CALL_PROCESS_KW, None)
    plan = monitor.before(kwargs, per_call, True)
    if plan is None:
        return target(*args, **kwargs)
    if not is_async:
        monitor.decide(plan)
    monitor.apply(plan, kwargs)
    t0 = time.perf_counter()
    try:
        inner = target(*args, **kwargs)
    except Exception as exc:
        monitor.finish(plan, usage=None, latency_ms=_elapsed_ms(t0), error=exc)
        raise
    manager = _AsyncStreamManager if is_async else _StreamManager
    return manager(monitor, plan, inner, t0)


def _class_method(resource: Any, name: str) -> Callable:
    """The class's current ``name`` bound to ``resource``, looked up per
    call so a later class patch still runs."""
    cls = type(resource)
    for klass in cls.__mro__:
        attr = vars(klass).get(name)
        if attr is not None:
            get = getattr(attr, "__get__", None)
            return get(resource, cls) if get is not None else attr
    raise AttributeError(name)


def _make_method(monitor: AnthropicMonitor, name: str, original: Callable, is_async: bool,
                 resource: Any = None) -> Callable:
    call = _call_stream if name == "stream" else _call_create

    def method(*args, **kwargs):
        target = original
        if resource is not None:
            try:
                target = _class_method(resource, name)
            except Exception:  # noqa: BLE001 - fail-open: the captured method
                logger.debug("monitor: class %s lookup failed", name, exc_info=True)
        return call(monitor, target, is_async, args, kwargs)

    method.__wrapped__ = original  # type: ignore[attr-defined]
    method.__tryaii_monitor__ = monitor  # type: ignore[attr-defined]
    method.__tryaii_resource__ = resource  # type: ignore[attr-defined]
    method.__name__ = getattr(original, "__name__", name)
    method.__doc__ = getattr(original, "__doc__", None)
    return method


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_anthropic_client(client: Any) -> bool:
    """True for a Messages-API client (``client.messages.create`` and no
    ``client.chat``)."""
    messages = getattr(client, "messages", None)
    return (messages is not None and callable(getattr(messages, "create", None))
            and getattr(client, "chat", None) is None)


def wrap_anthropic(client: Any, *, app: Optional[str] = None, process: Optional[str] = None,
                   mode: Optional[str] = None, route_fn: Optional[Callable] = None,
                   cachelint: bool = False, inline: Optional[bool] = None,
                   vectors: Optional[bool] = None) -> AnthropicMonitor:
    """Monitor every ``client.messages.create`` / ``client.messages.stream``
    call of an ``anthropic`` client (``Anthropic``, ``AsyncAnthropic``,
    ``AnthropicBedrock``, ``AnthropicVertex`` and their async twins).
    Arguments as :func:`tryaii.monitor.wrap`. Returns the monitor; the
    client is patched in place."""
    messages = client.messages
    transport = None
    if route_fn is not None or inline:
        transport = Transport(route_fn=route_fn, inline=True if inline is None else inline)
        transport.start()
    defaults = _wo._defaults
    monitor = AnthropicMonitor(
        app=app if app is not None else defaults["app"],
        process=process if process is not None else defaults["process"],
        mode=mode if mode is not None else defaults["mode"],
        transport=transport, cachelint=cachelint or defaults["cachelint"], vectors=vectors,
    )
    # Unwraps ``__wrapped__``: a re-wrapped or class-patched create still
    # reports the SDK's own ``async def``.
    is_async = _is_coroutine_function(messages.create)
    for name in ("create", "stream"):
        current = getattr(messages, name, None)
        if current is None:
            continue
        try:
            resource = None if name in vars(messages) else messages
        except TypeError:  # no __dict__
            resource = None
        original = current
        if getattr(current, "__tryaii_monitor__", None) is not None:
            original = current.__wrapped__  # re-wrapping replaces the earlier monitor
            resource = getattr(current, "__tryaii_resource__", None)
        setattr(messages, name, _make_method(monitor, name, original, is_async, resource))
    return monitor


_installed_monitors: dict = {}


def _monitor_for(resource: Any) -> Optional[AnthropicMonitor]:
    client = getattr(resource, "_client", None)
    if client is None:
        return None
    key = id(client)
    monitor = _installed_monitors.get(key)
    if monitor is None:
        defaults = _wo._defaults
        monitor = AnthropicMonitor(app=defaults["app"], process=defaults["process"],
                                   mode=defaults["mode"], cachelint=defaults["cachelint"])
        _installed_monitors[key] = monitor
    return monitor


def install_anthropic() -> bool:
    """Patch the ``anthropic`` SDK's ``Messages`` / ``AsyncMessages``
    classes (``create`` and ``stream``) with the defaults set by
    :func:`tryaii.monitor.install`. False when ``anthropic`` has not been
    imported yet."""
    if "anthropic" not in sys.modules:
        return False
    try:
        from anthropic.resources import messages as module  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return False
    patched = False
    for class_name in ("Messages", "AsyncMessages"):
        cls = getattr(module, class_name, None)
        if cls is None:
            continue
        create = getattr(cls, "create", None)
        if create is None:
            continue
        is_async = _is_coroutine_function(create)
        for name in ("create", "stream"):
            original = getattr(cls, name, None)
            if original is None or getattr(original, "__tryaii_patched__", False):
                continue

            def make(original=original, name=name, is_async=is_async):
                call = _call_stream if name == "stream" else _call_create

                def method(self, *args, **kwargs):
                    monitor = _monitor_for(self)
                    bound = original.__get__(self, type(self))
                    if monitor is None:
                        kwargs.pop(PER_CALL_PROCESS_KW, None)
                        return bound(*args, **kwargs)
                    return call(monitor, bound, is_async, args, kwargs)

                method.__tryaii_patched__ = True  # type: ignore[attr-defined]
                method.__wrapped__ = original  # type: ignore[attr-defined]
                method.__name__ = getattr(original, "__name__", name)
                method.__doc__ = getattr(original, "__doc__", None)
                return method

            setattr(cls, name, make())
        patched = True
    return patched


def wrap(client: Any, **kwargs: Any) -> Monitor:
    """:func:`tryaii.monitor.wrap`: an ``anthropic`` client goes to
    :func:`wrap_anthropic`, anything else to the openai wrapper."""
    if is_anthropic_client(client):
        return wrap_anthropic(client, **kwargs)
    return _wo.wrap(client, **kwargs)


def install(*, app: Optional[str] = None, process: Optional[str] = None,
            mode: Optional[str] = None, cachelint: bool = False) -> bool:
    """:func:`tryaii.monitor.install`: set the module defaults and patch
    every supported SDK already imported (``openai``, ``anthropic``). True
    when at least one was patched; import the SDK first, then install."""
    patched_openai = _wo.install(app=app, process=process, mode=mode, cachelint=cachelint)
    patched_anthropic = install_anthropic()
    return patched_openai or patched_anthropic


