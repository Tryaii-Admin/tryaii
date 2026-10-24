"""``tryaii.monitor.wrap`` / ``install`` for the ``openai`` SDK.

The wrapper replaces ``client.chat.completions.create`` on the instance
(duck-typed: ``openai`` is never imported here). Per call it

1. captures the call site and the sub-process name synchronously, before
   any ``await`` (``before``);
2. decides what to send: ``off`` keeps the developer's model, ``stable``
   uses the pinned model, ``dynamic`` routes in-path, ``managed`` sends the
   assigned model when it is reachable and policy-allowed (``decide``);
3. calls the original method OUTSIDE monitor code - provider exceptions
   propagate unchanged after an error event;
4. hands usage, latency and the plan to the transport worker, which
   shadow-routes (when not already routed) and writes the event and
   vector lines (``finish``).

Every monitor step is fail-open: an exception inside it is logged at
debug level and the developer's call proceeds as written.
"""

from __future__ import annotations

import inspect
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from tryaii.monitor import callsite, store
from tryaii.monitor.cache import CacheTracker
from tryaii.monitor.config import (
    env_from_os,
    resolve_app,
    resolve_mode,
    resolve_process,
)
from tryaii.monitor.context import current_process
from tryaii.monitor.events import (
    build_event,
    build_vector,
    classification_from_wire,
    estimate_cost,
    format_ts,
    usage_from_response,
)
from tryaii.monitor.extract import routing_text
from tryaii.monitor.pins import (
    evaluate_pin,
    model_facts,
    pin_from_first_call,
    pin_valid,
    set_pin,
)
from tryaii.monitor.transport import (
    STATUS_QUEUE_FULL,
    STATUS_SKIPPED,
    RouteOutcome,
    Transport,
    get_transport,
)
from tryaii.monitor.winner import (
    MODE_DYNAMIC,
    MODE_MANAGED,
    MODE_OFF,
    MODE_STABLE,
    PROVIDER_OPENAI,
    PROVIDER_OPENROUTER,
    managed_target,
    native_model_id,
    pick_winner,
    provider_class,
    reachable,
    resolve_anchor,
    route_allowed,
    routed_model,
    shadow_allowed,
)

logger = logging.getLogger("tryaii.monitor")

ENV_VECTORS = "TRYAII_MONITOR_VECTORS"
DOC_RECHECK_S = 5.0
PIN_HISTORY = timedelta(days=7)
PER_CALL_PROCESS_KW = "tryaii_process"


def _utcnow() -> datetime:
    """The wrapper's clock (tests monkeypatch this)."""
    return datetime.now(timezone.utc)


def vectors_enabled() -> bool:
    return os.environ.get(ENV_VECTORS, "1").strip() != "0"


class _CachedDoc:
    """A JSON document re-read when its mtime changes, checked at most
    every 5 s (so ``tryaii monitor set`` applies without a restart).

    ``version`` (optional) is an in-process change counter: when it moves,
    the document is re-read on the next ``get()`` regardless of the 5 s
    window or the mtime. ``remote.json`` uses it, so an answer the long poll
    just wrote applies on the very next call (TS-296)."""

    def __init__(self, path: Path, reader: Callable[[Path], Optional[dict]],
                 version: Optional[Callable[[], int]] = None):
        self._path = path
        self._reader = reader
        self._version = version
        self._doc: Optional[dict] = None
        self._loaded = False
        self._mtime: Optional[float] = None
        self._seen: Optional[int] = None
        self._checked_at = 0.0

    def _stat(self) -> Optional[float]:
        try:
            return self._path.stat().st_mtime
        except OSError:
            return None

    def get(self) -> Optional[dict]:
        version = self._version() if self._version is not None else None
        now = time.monotonic()
        if not self._loaded or version != self._seen:
            self._reload(now, version)
        elif now - self._checked_at >= DOC_RECHECK_S:
            self._checked_at = now
            if self._stat() != self._mtime:
                self._reload(now, version)
        return self._doc

    def _reload(self, now: float, version: Optional[int]) -> None:
        # Version and mtime are taken before the read: a write racing the
        # read is picked up on the next call, never lost.
        self._seen = version
        self._checked_at = now
        self._mtime = self._stat()
        self._doc = self._reader(self._path.parent)
        self._loaded = True

    def set(self, doc: dict) -> None:
        self._doc = doc
        self._loaded = True
        self._mtime = self._stat()
        if self._version is not None:
            self._seen = self._version()
        self._checked_at = time.monotonic()


@dataclass
class CallPlan:
    """Everything decided about one call before the provider is called."""

    ts: str
    app: str
    process: str
    mode: str
    provider: str
    model_requested: Optional[str]
    anchor: Optional[str]
    text: str
    prompt_hash: str
    call_site: Optional[dict]
    stream: bool
    #: Catalog id to send instead of the anchor (None = as written).
    target: Optional[str] = None
    #: Native id put in the request.
    model_sent: Optional[str] = None
    #: The shadow route's outcome (the recommendation); set early only when
    #: the in-path route used the same candidate list.
    outcome: Optional[RouteOutcome] = None
    #: The in-path route's outcome (dynamic / first stable call).
    routed_outcome: Optional[RouteOutcome] = None
    #: Managed model as configured (resolved in decide()).
    managed_model: Optional[str] = None
    #: Overrides the route status in the event (``unreachable_managed``).
    status_override: Optional[str] = None
    #: Policy-allowed ids (None = no policy), and the candidate lists of the
    #: in-path and the shadow route requests (SPEC.md §2.8).
    policy: Optional[list] = None
    route_allowed: Optional[list] = None
    shadow_allowed: Optional[list] = None
    lint_key: Optional[str] = None
    #: The linted request (model, a snapshot of its messages and its tool
    #: schemas: ``tools`` then legacy ``functions``), kept for the event's
    #: ``cache`` block (API.md v1.4 §3.2.1).
    lint_model: Optional[str] = None
    lint_messages: Optional[list] = None
    lint_tools: Optional[list] = None
    #: Where ``mode`` came from (SPEC.md §2.6: env, remote, args, config, default).
    mode_source: Optional[str] = None
    extra: dict = field(default_factory=dict)


class Monitor:
    """Per-client monitor state: identity, mode sources, store and transport."""

    def __init__(self, provider: str, *, app: Optional[str] = None,
                 process: Optional[str] = None, mode: Optional[str] = None,
                 transport: Optional[Transport] = None, cachelint: bool = False,
                 vectors: Optional[bool] = None):
        self.provider = provider
        self._transport = transport or get_transport()
        env = env_from_os()
        try:
            cwd_name = Path.cwd().name
        except OSError:
            cwd_name = None
        self.app = resolve_app(env=env, args=app, cwd_name=cwd_name)["app"]
        self._default_process = process
        self._mode_arg = mode
        self.dir = store.app_dir(self._transport.data_dir, self.app)
        self._config = _CachedDoc(self.dir / store.CONFIG_FILE, store.read_config)
        self._remote = _CachedDoc(self.dir / store.REMOTE_FILE, store.read_remote,
                                  store.remote_generation)
        self._pins = _CachedDoc(self.dir / store.PINS_FILE, store.read_pins)
        try:
            self._transport.register_app(self.dir)
        except Exception:  # noqa: BLE001 - fail-open
            logger.debug("monitor: register_app failed", exc_info=True)
        self._vectors = vectors_enabled() if vectors is None else bool(vectors)
        self._hook = None
        self._cache: Optional[CacheTracker] = None
        if cachelint:
            from tryaii.cachelint.hook import CacheLintHook

            self._hook = CacheLintHook()
            self._cache = CacheTracker()

    @property
    def transport(self) -> Transport:
        return self._transport

    # -- 1. capture ------------------------------------------------------

    def before(self, kwargs: dict, per_call_process: Any, stream: bool) -> Optional[CallPlan]:
        """Identity and routing text for this call; None on any failure."""
        try:
            call_site = callsite.capture()
            process = resolve_process(per_call=per_call_process, context=current_process(),
                                      default=self._default_process, call_site=call_site)["process"]
            resolved = resolve_mode(process, env=env_from_os(), remote=self._remote.get(),
                                    args=self._mode_arg, config=self._config.get())
            model = kwargs.get("model")
            model_requested = model if isinstance(model, str) else None
            registry = self._transport.registry()
            anchor = resolve_anchor(model_requested, registry) if model_requested else None
            text, digest = routing_text(kwargs.get("messages"))
            policy = self._transport.policy_allowed()
            ids = registry.model_ids
            return CallPlan(
                ts=format_ts(_utcnow()), app=self.app, process=process,
                mode=resolved["mode"], provider=self.provider,
                model_requested=model_requested, anchor=anchor,
                text=text, prompt_hash=digest, call_site=call_site, stream=bool(stream),
                model_sent=model_requested, managed_model=resolved["model"],
                policy=policy, mode_source=resolved["source"],
                route_allowed=route_allowed(self.provider, ids, anchor, policy),
                shadow_allowed=shadow_allowed(ids, policy),
            )
        except Exception:  # noqa: BLE001 - fail-open
            logger.debug("monitor: before() failed", exc_info=True)
            return None

    # -- 1b. first-call remote config (SPEC.md §3.3, TS-296) --------------

    def remote_pending(self, plan: CallPlan) -> bool:
        """True while this process has not yet had its first remote-config
        answer (and its bounded first-call wait has not run out). Cheap: a
        flag check once settled. Never when the env pins the mode."""
        if plan.mode_source == "env":
            return False
        pending = getattr(self._transport, "remote_pending", None)
        try:
            return bool(pending()) if pending is not None else False
        except Exception:  # noqa: BLE001 - fail-open
            return False

    def settle_mode(self, plan: CallPlan) -> None:
        """Wait (bounded, once per process) for the first remote-config
        answer, then re-resolve the plan's mode with it. Never raises."""
        try:
            self._transport.wait_remote_config()
            resolved = resolve_mode(plan.process, env=env_from_os(), remote=self._remote.get(),
                                    args=self._mode_arg, config=self._config.get())
            plan.mode = resolved["mode"]
            plan.managed_model = resolved["model"]
            plan.mode_source = resolved["source"]
        except Exception:  # noqa: BLE001 - fail-open: keep the mode from before()
            logger.debug("monitor: settle_mode() failed", exc_info=True)

    # -- 2. decide -------------------------------------------------------

    def needs_decision(self, plan: CallPlan) -> bool:
        if plan.mode == MODE_MANAGED:
            return True
        return plan.mode != MODE_OFF and bool(plan.text)

    def decide(self, plan: CallPlan) -> None:
        """Pick ``plan.target`` per SPEC.md §2.4-§2.5, §2.9. May route
        in-path (dynamic, first stable call) with the reachable ∩ policy
        candidate list."""
        try:
            if not self.needs_decision(plan):
                return
            if plan.mode == MODE_DYNAMIC:
                winner = self._route_in_path(plan)
                plan.target = routed_model(MODE_DYNAMIC, plan.provider, plan.anchor, winner,
                                           None, allowed=plan.policy)
            elif plan.mode == MODE_STABLE:
                plan.target = self._stable_target(plan)
            elif plan.mode == MODE_MANAGED:
                decision = managed_target(plan.provider, plan.anchor, plan.managed_model,
                                          self._transport.registry(), allowed=plan.policy)
                plan.target = decision["target"]
                plan.status_override = decision["route_status"]
        except Exception:  # noqa: BLE001 - fail-open: the anchor is sent
            logger.debug("monitor: decide() failed", exc_info=True)
            plan.target = None

    def _route_in_path(self, plan: CallPlan) -> Optional[str]:
        """Route this call in-path over the candidates it could actually be
        sent to (reachable ∩ policy) and return that ranking's best-for-less
        model. Skipped (None) when nothing but the anchor could be sent. The
        outcome doubles as the shadow outcome when both lists are equal."""
        candidates = plan.route_allowed
        if candidates is not None and not (set(candidates) - {plan.anchor}):
            return None
        outcome = self._route(plan, in_path=True, allowed=candidates)
        plan.routed_outcome = outcome
        if candidates == plan.shadow_allowed:
            plan.outcome = outcome
        return self._winner(outcome, plan.anchor)

    def _stable_target(self, plan: CallPlan) -> Optional[str]:
        pins = self._pins.get()
        pin = (pins.get("processes") or {}).get(plan.process)
        if pin_valid(pin, plan.ts):
            return routed_model(MODE_STABLE, plan.provider, plan.anchor, None, pin["model"],
                                allowed=plan.policy)
        events = store.read_events(self.dir, plan.ts, PIN_HISTORY.total_seconds())
        decision = evaluate_pin(events, pin, plan.ts, plan.process, self._model_facts())
        if decision["action"] == "pin":
            new_pin = decision["pin"]
        else:
            # No usable history: route this very call and pin its winner.
            winner = self._route_in_path(plan)
            if not winner or not reachable(plan.provider, winner, plan.anchor):
                return None
            new_pin = pin_from_first_call(winner, plan.ts)
        doc = set_pin(pins, plan.process, new_pin)
        store.write_pins(self.dir, doc)
        self._pins.set(doc)
        return routed_model(MODE_STABLE, plan.provider, plan.anchor, None, new_pin["model"],
                            allowed=plan.policy)

    def _model_facts(self) -> Optional[dict]:
        """The catalog's benchmark score and price per model, the pin
        tie-break inputs (SPEC.md §2.5); None (ids only) if unavailable."""
        try:
            return model_facts(self._transport.bundle())
        except Exception:  # noqa: BLE001 - fail-open
            logger.debug("monitor: catalog facts unavailable for the pin tie-break", exc_info=True)
            return None

    def _route(self, plan: CallPlan, *, in_path: bool, allowed: Optional[list],
               include_embedding: Optional[bool] = None) -> RouteOutcome:
        embed = self._vectors if include_embedding is None else include_embedding
        return self._transport.route(plan.text, include_embedding=embed, in_path=in_path,
                                     allowed_models=allowed)

    def _winner(self, outcome: Optional[RouteOutcome], anchor: Optional[str]) -> Optional[str]:
        if outcome is None or not outcome.ok:
            return None
        return pick_winner(outcome.scores, anchor, self._transport.registry())["winner"]

    # -- 3. apply to the request ----------------------------------------

    def apply(self, plan: CallPlan, kwargs: dict) -> None:
        """Rewrite ``model`` when a target was chosen; ask streams for usage."""
        try:
            if plan.target:
                plan.model_sent = native_model_id(plan.provider, plan.target)
                kwargs["model"] = plan.model_sent
            if plan.stream and plan.provider in (PROVIDER_OPENAI, PROVIDER_OPENROUTER) \
                    and "stream_options" not in kwargs:
                kwargs["stream_options"] = {"include_usage": True}
            if self._hook is not None:
                model = kwargs.get("model") or ""
                messages = kwargs.get("messages") or []
                plan.lint_key = self._hook.preflight(model, messages)
                if plan.lint_key is not None and isinstance(messages, list):
                    # Snapshot: the caller may append to its list before
                    # the worker records the event.
                    plan.lint_model = str(model)
                    plan.lint_messages = [dict(m) if isinstance(m, dict) else m
                                          for m in messages]
                    tools = kwargs.get("tools")
                    functions = kwargs.get("functions")
                    schemas = [*(tools if isinstance(tools, list) else []),
                               *(functions if isinstance(functions, list) else [])]
                    plan.lint_tools = schemas or None
        except Exception:  # noqa: BLE001
            logger.debug("monitor: apply() failed", exc_info=True)

    # -- 4. record -------------------------------------------------------

    def finish(self, plan: CallPlan, *, usage: Any, latency_ms: Optional[int],
               error: Optional[BaseException]) -> None:
        """Hand the finished call to the worker (never raises)."""
        try:
            if self._hook is not None and plan.lint_key and isinstance(usage, dict):
                self._hook.verify(plan.lint_key, usage)
            usage_dict = usage_from_response(usage)
            error_dict = _error_dict(error)
            if error_dict is not None:
                # A failed call is recorded right away and never routed:
                # nothing to learn from a prompt the provider rejected, and
                # the record must survive a process that exits on the error.
                self._write(plan, plan.outcome or RouteOutcome(STATUS_SKIPPED),
                            usage_dict, latency_ms, error_dict)
                return

            def job() -> None:
                outcome = plan.outcome
                if outcome is None:
                    if plan.text:
                        # Ask for the embedding only if the in-path route
                        # did not already bring one.
                        routed = plan.routed_outcome
                        have = routed is not None and isinstance(routed.embedding, dict)
                        outcome = self._route(plan, in_path=False,
                                              allowed=plan.shadow_allowed,
                                              include_embedding=self._vectors and not have)
                    else:
                        outcome = RouteOutcome(STATUS_SKIPPED)
                self._write(plan, outcome, usage_dict, latency_ms, error_dict)

            if not self._transport.submit(job):
                self._write(plan, plan.outcome or RouteOutcome(STATUS_QUEUE_FULL),
                            usage_dict, latency_ms, error_dict)
        except Exception:  # noqa: BLE001
            logger.debug("monitor: finish() failed", exc_info=True)

    def _write(self, plan: CallPlan, outcome: RouteOutcome, usage: Optional[dict],
               latency_ms: Optional[int], error: Optional[dict]) -> None:
        registry = self._transport.registry()
        winner = None
        winner_reachable = False
        classification = None
        if outcome.ok:
            winner = pick_winner(outcome.scores, plan.anchor, registry,
                                 allowed=plan.policy)["winner"]
            winner_reachable = reachable(plan.provider, winner, plan.anchor)
            classification = classification_from_wire(outcome.classification)
        elif plan.routed_outcome is not None and plan.routed_outcome.ok:
            classification = classification_from_wire(plan.routed_outcome.classification)
        used_catalog = plan.target or plan.anchor
        embedding = outcome.embedding
        if not isinstance(embedding, dict) and plan.routed_outcome is not None:
            embedding = plan.routed_outcome.embedding
        vector_written = False
        if self._vectors and plan.text and isinstance(embedding, dict):
            try:
                record = build_vector(prompt_hash=plan.prompt_hash, ts=plan.ts,
                                      process=plan.process,
                                      model=str(embedding.get("model") or ""),
                                      vector=embedding.get("v") or [])
                vector_written = store.append_vector(self.dir, record)
            except Exception:  # noqa: BLE001 - the event is still worth writing
                logger.debug("monitor: vector write failed", exc_info=True)
        event = build_event(
            ts=plan.ts, app=plan.app, process=plan.process, call_site=plan.call_site,
            provider=plan.provider, model_requested=plan.model_requested,
            model_resolved=plan.anchor, model_used=plan.model_sent, mode=plan.mode,
            winner=winner, winner_reachable=winner_reachable,
            route_status=plan.status_override or outcome.status,
            classification=classification, usage=usage,
            est_cost_usd=estimate_cost(registry, used_catalog, usage),
            est_cost_winner_usd=estimate_cost(registry, winner, usage),
            est_cost_anchor_usd=estimate_cost(registry, plan.anchor, usage),
            latency_ms=latency_ms, stream=plan.stream, error=error,
            prompt_hash=plan.prompt_hash, vector=vector_written,
            taxonomy_version=self._transport.taxonomy_version(),
            cache=self._cache_block(plan),
        )
        store.append_event(self.dir, event)

    def _cache_block(self, plan: CallPlan) -> Optional[dict]:
        """``{"outcome", "report"}`` when this call was linted, else None."""
        if self._cache is None or plan.lint_messages is None:
            return None
        try:
            return self._cache.observe(plan.process, plan.lint_model or "", plan.lint_messages,
                                       tools=plan.lint_tools)
        except Exception:  # noqa: BLE001 - fail-open: the event is still written
            logger.debug("monitor: cache block failed", exc_info=True)
            return None
        finally:
            plan.lint_messages = None
            plan.lint_tools = None


def _error_dict(exc: Optional[BaseException]) -> Optional[dict]:
    if exc is None:
        return None
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int) or isinstance(status, bool):
        status = None
    return {"status": status, "class": type(exc).__name__}


def _raw_usage(response: Any) -> Any:
    usage = getattr(response, "usage", None)
    if usage is None or isinstance(usage, dict):
        return usage
    dump = getattr(usage, "model_dump", None)
    if callable(dump):
        try:
            return dump()
        except Exception:  # noqa: BLE001
            return usage
    return usage


def _elapsed_ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


# ---------------------------------------------------------------------------
# Stream wrappers
# ---------------------------------------------------------------------------

class _StreamBase:
    """Forwards every chunk and records the call once the stream ends
    (exhausted, closed, or failed). ``usage`` arrives on the last chunk when
    ``stream_options.include_usage`` is on."""

    def __init__(self, inner: Any, done: Callable[[Any, Optional[BaseException]], None]):
        self._inner = inner
        self._done = done
        self._usage: Any = None
        self._finished = False

    def _note(self, chunk: Any) -> Any:
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            self._usage = usage
        return chunk

    def _finish(self, exc: Optional[BaseException] = None) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            self._done(self._usage, exc)
        except Exception:  # noqa: BLE001
            logger.debug("monitor: stream record failed", exc_info=True)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class MonitoredStream(_StreamBase):
    def __iter__(self):
        return self

    def __next__(self):
        try:
            chunk = next(self._iter())
        except StopIteration:
            self._finish()
            raise
        except BaseException as exc:
            self._finish(exc)
            raise
        return self._note(chunk)

    def _iter(self):
        it = self.__dict__.get("_it")
        if it is None:
            it = iter(self._inner)
            self.__dict__["_it"] = it
        return it

    def __enter__(self):
        enter = getattr(self._inner, "__enter__", None)
        if enter is not None:
            enter()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._finish(exc)
        exit_ = getattr(self._inner, "__exit__", None)
        if exit_ is not None:
            return exit_(exc_type, exc, tb)
        return False

    def close(self) -> None:
        self._finish()
        close = getattr(self._inner, "close", None)
        if close is not None:
            close()


class AsyncMonitoredStream(_StreamBase):
    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            chunk = await self._iter().__anext__()
        except StopAsyncIteration:
            self._finish()
            raise
        except BaseException as exc:
            self._finish(exc)
            raise
        return self._note(chunk)

    def _iter(self):
        it = self.__dict__.get("_it")
        if it is None:
            it = self._inner.__aiter__()
            self.__dict__["_it"] = it
        return it

    async def __aenter__(self):
        enter = getattr(self._inner, "__aenter__", None)
        if enter is not None:
            await enter()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._finish(exc)
        exit_ = getattr(self._inner, "__aexit__", None)
        if exit_ is not None:
            return await exit_(exc_type, exc, tb)
        return False

    async def aclose(self) -> None:
        self._finish()
        close = getattr(self._inner, "aclose", None) or getattr(self._inner, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result


# ---------------------------------------------------------------------------
# The create() wrappers
# ---------------------------------------------------------------------------

def _is_coroutine_function(fn: Any) -> bool:
    """True for an ``async def``, also behind sync decorators that set
    ``__wrapped__`` (the real ``AsyncCompletions.create`` is one)."""
    fn = getattr(fn, "__func__", fn)
    if inspect.iscoroutinefunction(fn):
        return True
    try:
        inner = inspect.unwrap(fn)
    except Exception:  # noqa: BLE001 - a cyclic or broken __wrapped__ chain
        return False
    return inspect.iscoroutinefunction(getattr(inner, "__func__", inner))


def _class_create(resource: Any) -> Callable:
    """The class's current ``create`` bound to ``resource``, looked up on
    every call so a class patch applied after ``wrap()`` (``import
    langfuse.openai``) still runs."""
    cls = type(resource)
    for klass in cls.__mro__:
        attr = vars(klass).get("create")
        if attr is not None:
            get = getattr(attr, "__get__", None)
            return get(resource, cls) if get is not None else attr
    raise AttributeError("create")


def _run_sync(monitor: Monitor, original: Callable, plan: Optional[CallPlan],
              args: tuple, kwargs: dict) -> Any:
    if plan is not None:
        if monitor.remote_pending(plan):
            monitor.settle_mode(plan)
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
        return MonitoredStream(
            response,
            lambda usage, exc: monitor.finish(plan, usage=_raw_usage_value(usage),
                                              latency_ms=_elapsed_ms(t0), error=exc))
    monitor.finish(plan, usage=_raw_usage(response), latency_ms=_elapsed_ms(t0), error=None)
    return response


async def _run_async(monitor: Monitor, original: Callable, plan: Optional[CallPlan],
                     args: tuple, kwargs: dict) -> Any:
    if plan is not None:
        if monitor.remote_pending(plan):
            import asyncio

            # Off the event loop: the first call of the process may wait
            # (bounded) for the first remote-config answer.
            await asyncio.get_running_loop().run_in_executor(None, monitor.settle_mode, plan)
        if monitor.needs_decision(plan):
            import asyncio

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, monitor.decide, plan)
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
        return AsyncMonitoredStream(
            response,
            lambda usage, exc: monitor.finish(plan, usage=_raw_usage_value(usage),
                                              latency_ms=_elapsed_ms(t0), error=exc))
    monitor.finish(plan, usage=_raw_usage(response), latency_ms=_elapsed_ms(t0), error=None)
    return response


def _raw_usage_value(usage: Any) -> Any:
    """A stream chunk's usage object → dict when it can dump itself."""
    if usage is None or isinstance(usage, dict):
        return usage
    dump = getattr(usage, "model_dump", None)
    if callable(dump):
        try:
            return dump()
        except Exception:  # noqa: BLE001
            return usage
    return usage


def _make_create(monitor: Monitor, original: Callable, is_async: bool,
                 resource: Any = None) -> Callable:
    # A plain ``def`` even for async clients, so the call site and the
    # process context are captured before the first ``await``. With
    # ``resource`` the class's ``create`` is looked up per call instead of
    # calling ``original`` (sync/async is fixed by the client's class).
    def create(*args, **kwargs):
        target = original
        if resource is not None:
            try:
                target = _class_create(resource)
            except Exception:  # noqa: BLE001 - fail-open: the captured method
                logger.debug("monitor: class create lookup failed", exc_info=True)
        per_call = kwargs.pop(PER_CALL_PROCESS_KW, None)
        stream = bool(kwargs.get("stream"))
        plan = monitor.before(kwargs, per_call, stream)
        if is_async:
            return _run_async(monitor, target, plan, args, kwargs)
        return _run_sync(monitor, target, plan, args, kwargs)

    create.__wrapped__ = original  # type: ignore[attr-defined]
    create.__tryaii_monitor__ = monitor  # type: ignore[attr-defined]
    create.__tryaii_resource__ = resource  # type: ignore[attr-defined]
    create.__name__ = getattr(original, "__name__", "create")
    create.__doc__ = getattr(original, "__doc__", None)
    return create


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_defaults: dict = {"app": None, "process": None, "mode": None, "cachelint": False}


def _client_provider(client: Any) -> str:
    base_url = getattr(client, "base_url", None)
    return provider_class(str(base_url) if base_url is not None else None)


def wrap(client: Any, *, app: Optional[str] = None, process: Optional[str] = None,
         mode: Optional[str] = None, route_fn: Optional[Callable] = None,
         cachelint: bool = False, inline: Optional[bool] = None,
         vectors: Optional[bool] = None) -> Monitor:
    """Monitor every ``client.chat.completions.create`` call of ``client``.

    ``client`` is an ``openai.OpenAI`` / ``AsyncOpenAI`` (or anything with
    ``base_url`` and ``chat.completions.create``). ``app`` / ``process`` /
    ``mode`` set this client's defaults (SPEC.md §2.6). ``route_fn`` and
    ``inline`` are the test seam: a function replacing the daemon, run
    synchronously. Returns the :class:`Monitor`; the client is patched in
    place.
    """
    completions = client.chat.completions
    try:
        # No own ``create`` on the instance: follow the class's method per
        # call, so a later class patch (``import langfuse.openai``) runs too.
        # An instance someone wrapped before us keeps that wrapper.
        resource = None if "create" in vars(completions) else completions
    except TypeError:  # no __dict__
        resource = None
    original = completions.create
    previous = getattr(original, "__wrapped__", None)
    if previous is not None and getattr(original, "__tryaii_monitor__", None) is not None:
        original = previous  # re-wrapping replaces the earlier monitor
        resource = getattr(completions.create, "__tryaii_resource__", None)
    transport = None
    if route_fn is not None or inline:
        transport = Transport(route_fn=route_fn, inline=True if inline is None else inline)
        transport.start()
    monitor = Monitor(
        _client_provider(client),
        app=app if app is not None else _defaults["app"],
        process=process if process is not None else _defaults["process"],
        mode=mode if mode is not None else _defaults["mode"],
        transport=transport, cachelint=cachelint or _defaults["cachelint"],
        vectors=vectors,
    )
    create = _make_create(monitor, original, _is_coroutine_function(original), resource)
    setattr(completions, "create", create)
    return monitor


_installed_monitors: dict = {}


def _monitor_for(resource: Any) -> Optional[Monitor]:
    client = getattr(resource, "_client", None)
    if client is None:
        return None
    key = id(client)
    monitor = _installed_monitors.get(key)
    if monitor is None:
        monitor = Monitor(_client_provider(client), app=_defaults["app"],
                          process=_defaults["process"], mode=_defaults["mode"],
                          cachelint=_defaults["cachelint"])
        _installed_monitors[key] = monitor
    return monitor


def install(*, app: Optional[str] = None, process: Optional[str] = None,
            mode: Optional[str] = None, cachelint: bool = False) -> bool:
    """Set module defaults and patch the ``openai`` SDK classes so every
    client created in this process is monitored.

    Returns False (defaults still set) when ``openai`` has not been imported
    yet: import it first, then call ``install()``.
    """
    _defaults.update({"app": app, "process": process, "mode": mode, "cachelint": cachelint})
    if "openai" not in sys.modules:
        return False
    try:
        from openai.resources.chat import completions as module  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return False
    patched = False
    for class_name in ("Completions", "AsyncCompletions"):
        cls = getattr(module, class_name, None)
        if cls is None:
            continue
        original = getattr(cls, "create", None)
        if original is None or getattr(original, "__tryaii_patched__", False):
            patched = True
            continue
        is_async = _is_coroutine_function(original)

        def make(original=original, is_async=is_async):
            def create(self, *args, **kwargs):
                monitor = _monitor_for(self)
                bound = original.__get__(self, type(self))
                if monitor is None:
                    kwargs.pop(PER_CALL_PROCESS_KW, None)
                    return bound(*args, **kwargs)
                per_call = kwargs.pop(PER_CALL_PROCESS_KW, None)
                stream = bool(kwargs.get("stream"))
                plan = monitor.before(kwargs, per_call, stream)
                if is_async:
                    return _run_async(monitor, bound, plan, args, kwargs)
                return _run_sync(monitor, bound, plan, args, kwargs)

            create.__tryaii_patched__ = True  # type: ignore[attr-defined]
            create.__wrapped__ = original  # type: ignore[attr-defined]
            return create

        setattr(cls, "create", make())
        patched = True
    return patched
