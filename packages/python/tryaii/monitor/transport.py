"""Routing transport for the monitor wrapper: the warm daemon, off the hot path.

One :class:`Transport` per process owns a worker thread and a bounded queue.
Shadow routing and record writing run on the worker; ``dynamic`` calls (and
a ``stable`` process without a usable pin) route in-path with a short
budget, and only through a daemon that is already known to be live - the
wrapper never waits for a spawn and never loads the embedding model in the
application's process.

Test seam: ``Transport(route_fn=..., inline=True)`` replaces the daemon with a
function ``route_fn(text) -> (scores, classification, embedding | None)`` and
runs every job synchronously.
"""

from __future__ import annotations

import atexit
import logging
import os
import queue
import socket
import threading
import time
from typing import Any, Callable, Optional

from tryaii.config import TryaiiDreConfig
from tryaii.scoring.priorities import DEFAULT_PRIORITIES

logger = logging.getLogger("tryaii.monitor")

QUEUE_MAX = 256
#: Every ranked model: the winner rule needs the anchor's row (SPEC.md §2.3).
TOP_K = 1000
ENV_ROUTE_TIMEOUT_MS = "TRYAII_MONITOR_ROUTE_TIMEOUT_MS"
DEFAULT_ROUTE_TIMEOUT_MS = 1500
SHADOW_TIMEOUT_S = 2.0
STATE_RECHECK_S = 5.0
DRAIN_TIMEOUT_S = 2.0
#: The first call of a process waits at most this long, once, for the first
#: remote-config answer (SPEC.md §3.3, TS-296).
REMOTE_FIRST_WAIT_S = 0.5

STATUS_OK = "ok"
STATUS_UNAVAILABLE = "unavailable"
STATUS_TIMEOUT = "timeout"
STATUS_DISABLED = "disabled"
STATUS_QUEUE_FULL = "queue_full"
STATUS_SKIPPED = "skipped"

_UNSET = object()
_BROKEN = object()


class RouteOutcome:
    """What one routing attempt produced (``status`` per SPEC.md §3.1)."""

    __slots__ = ("status", "scores", "classification", "embedding")

    def __init__(self, status: str, scores: Optional[list] = None,
                 classification: Any = None, embedding: Optional[dict] = None):
        self.status = status
        self.scores = scores or []
        self.classification = classification
        self.embedding = embedding

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


def in_path_timeout_s() -> float:
    raw = os.environ.get(ENV_ROUTE_TIMEOUT_MS, "")
    try:
        ms = int(raw) if raw else DEFAULT_ROUTE_TIMEOUT_MS
    except ValueError:
        ms = DEFAULT_ROUTE_TIMEOUT_MS
    return max(ms, 1) / 1000.0


class Transport:
    def __init__(self, config: Optional[TryaiiDreConfig] = None, *,
                 route_fn: Optional[Callable] = None, inline: bool = False,
                 policy: Any = "auto"):
        self._config = config or TryaiiDreConfig()
        self._route_fn = route_fn
        self._inline = inline
        self._queue: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._bundle = None
        self._registry = None
        self._state: Optional[dict] = None
        self._state_checked_at = 0.0
        self._started = False
        # Set by the exit hook: routing is over, pending records are
        # written at once as ``unavailable`` (TS-224).
        self._closing = False
        # Data-save policy (SPEC.md §2.8): "auto" = the saved policy, loaded
        # once per process; None = no policy; a ModelPolicy/dict = that one;
        # a list/tuple/set of ids = the allowed ids as is (test seam).
        self._policy_spec = policy
        self._policy = _UNSET
        self._policy_cache: Optional[tuple] = None
        self._uploader = None
        self._remote = None
        #: The Langfuse exporter (SPEC.md "Langfuse export"), or None.
        self._langfuse = None
        #: True once the first remote-config answer arrived or the bounded
        #: first-call wait ran out: calls never wait again.
        self._remote_settled = False
        self._remote_deadline: Optional[float] = None
        self._app_dirs: list = []

    # -- catalog ---------------------------------------------------------

    @property
    def config(self) -> TryaiiDreConfig:
        return self._config

    def bundle(self):
        """The catalog bundle in use (starter until warm-up)."""
        self.registry()
        return self._bundle

    def taxonomy_version(self) -> Optional[str]:
        from tryaii.monitor.events import taxonomy_version

        try:
            return taxonomy_version(self.bundle())
        except Exception:  # noqa: BLE001
            return None

    def policy_allowed(self) -> Optional[list]:
        """The policy-allowed catalog ids for the current bundle, or None
        (no policy). Evaluated once per bundle; a broken saved policy fails
        closed (``[]``). Pure: no embedding model, no daemon."""
        spec = self._policy_spec
        if isinstance(spec, (list, tuple, set, frozenset)):
            return list(spec)
        from tryaii.monitor.winner import policy_allowed

        if self._policy is _UNSET:
            from tryaii import policy as pol

            try:
                self._policy = pol.resolve_policy(spec)
            except pol.PolicyError:
                logger.debug("monitor: saved policy unreadable; allowing nothing",
                             exc_info=True)
                self._policy = _BROKEN
        registry = self.registry()
        bundle = self._bundle
        cache = self._policy_cache
        if cache is not None and cache[0] is bundle:
            return cache[1]
        if self._policy is _BROKEN:
            allowed: Optional[list] = []
        else:
            allowed = policy_allowed(self._policy, getattr(bundle, "origins", None),
                                     registry.model_ids)
        self._policy_cache = (bundle, allowed)
        return allowed

    # -- upload + remote config (API.md §3) ------------------------------

    def register_app(self, directory) -> None:
        """An app directory this process records to: uploaded and kept in
        sync with the remote config when those services run."""
        with self._lock:
            if directory not in self._app_dirs:
                self._app_dirs.append(directory)
            uploader, remote, langfuse = self._uploader, self._remote, self._langfuse
        for service in (uploader, remote, langfuse):
            if service is not None:
                service.register(directory)

    def remote_pending(self) -> bool:
        """True while a remote poller runs whose first answer has not come
        and whose first-call wait has not run out (SPEC.md §3.3). A flag
        check once settled; False without a poller."""
        if self._remote_settled:
            return False
        remote = self._remote
        if remote is None:
            return False
        if remote.first_answer.is_set():
            self._remote_settled = True
            return False
        return True

    def wait_remote_config(self) -> None:
        """Block until the poller's first answer (200/304/error/401) or until
        ``REMOTE_FIRST_WAIT_S`` after the first call that waited, whichever
        comes first; concurrent first calls share the one deadline. After
        that no call waits again."""
        remote = self._remote
        if self._remote_settled or remote is None:
            return
        with self._lock:
            if self._remote_deadline is None:
                self._remote_deadline = time.monotonic() + REMOTE_FIRST_WAIT_S
            deadline = self._remote_deadline
        remaining = deadline - time.monotonic()
        if remaining > 0:
            remote.first_answer.wait(remaining)
        self._remote_settled = True

    def _start_services(self) -> None:
        """Start the uploader and the remote poller when TRYAII_API_KEY is
        set (each unless disabled by its env switch), and the Langfuse
        exporter when its settings say on. Never raises."""
        self._start_langfuse()
        try:
            from tryaii.monitor import api
            from tryaii.monitor.remote import RemotePoller, remote_enabled
            from tryaii.monitor.upload import Uploader, upload_enabled

            key = api.api_key()
            if not key:
                return
            base = api.api_url()
            uploader = Uploader(key, base) if upload_enabled() else None
            remote = RemotePoller(key, base) if remote_enabled() else None
            with self._lock:
                self._uploader, self._remote = uploader, remote
                dirs = list(self._app_dirs)
            for service in (uploader, remote):
                if service is None:
                    continue
                for directory in dirs:
                    service.register(directory)
                service.start()
        except Exception:  # noqa: BLE001 - fail-open
            logger.debug("monitor: upload/remote services failed to start", exc_info=True)

    def _start_langfuse(self) -> None:
        try:
            from tryaii.monitor.langfuse import Exporter

            exporter = Exporter.from_env()
            if exporter is None:
                return
            with self._lock:
                self._langfuse = exporter
                dirs = list(self._app_dirs)
            for directory in dirs:
                exporter.register(directory)
            exporter.start()
        except Exception:  # noqa: BLE001 - fail-open
            logger.debug("monitor: the Langfuse exporter failed to start", exc_info=True)

    @property
    def data_dir(self):
        return self._config.data_dir

    def registry(self):
        """The pricing registry: the starter catalog until warm-up selects
        the user's catalog (full after login)."""
        with self._lock:
            if self._registry is None:
                from tryaii.catalog.bundle import starter_bundle
                from tryaii.registry.models import ModelRegistry

                self._bundle = starter_bundle()
                self._registry = ModelRegistry.from_bundle(self._bundle)
            return self._registry

    def _set_bundle(self, bundle) -> None:
        from tryaii.registry.models import ModelRegistry

        registry = ModelRegistry.from_bundle(bundle)
        with self._lock:
            self._bundle = bundle
            self._registry = registry

    # -- worker ----------------------------------------------------------

    def start(self) -> None:
        """Start the worker (once) and queue the warm-up job."""
        with self._lock:
            if self._started:
                return
            self._started = True
        if self._route_fn is not None or self._inline:
            return
        self._thread = threading.Thread(target=self._run, name="tryaii-monitor", daemon=True)
        self._thread.start()
        atexit.register(self._at_exit)
        self.submit(self._warm_up)
        self._start_services()

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            try:
                job()
            except Exception:  # noqa: BLE001 - a bad job must not kill the worker
                logger.debug("monitor job failed", exc_info=True)
            finally:
                self._queue.task_done()

    def submit(self, job: Callable[[], None]) -> bool:
        """Queue ``job``; False when the queue is full (the caller then
        records the call immediately with ``queue_full``)."""
        if self._inline or self._thread is None:
            try:
                job()
            except Exception:  # noqa: BLE001
                logger.debug("monitor job failed", exc_info=True)
            return True
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            return False
        return True

    def _at_exit(self) -> None:
        """Process exit: finish queued records (<= 2 s); whatever is still
        queued (the worker may be inside a slow warm-up) is recorded now as
        ``unavailable``; then one last upload pass (<= 2 s) and one last
        Langfuse pass (<= 2 s)."""
        self.drain(DRAIN_TIMEOUT_S)
        self.flush_pending()
        uploader = self._uploader
        if uploader is not None:
            uploader.drain(DRAIN_TIMEOUT_S)
        langfuse = self._langfuse
        if langfuse is not None:
            langfuse.drain(DRAIN_TIMEOUT_S)

    def flush_pending(self) -> None:
        """Run the queued jobs on the calling thread with routing switched
        off: each pending call is recorded at once with ``route_status:
        unavailable`` instead of being lost when the process exits while the
        worker is still warming the daemon up (TS-224)."""
        self._closing = True
        if self._thread is None:
            return
        while True:
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                job()
            except Exception:  # noqa: BLE001 - one bad record must not stop the flush
                logger.debug("monitor job failed", exc_info=True)
            finally:
                self._queue.task_done()

    def drain(self, timeout: float = DRAIN_TIMEOUT_S) -> None:
        """Wait up to ``timeout`` seconds for queued jobs (process exit)."""
        if self._thread is None:
            return
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.02)

    def _warm_up(self) -> None:
        """Pick the catalog (may do the daily server check) and find or
        start the daemon - both off the hot path."""
        if self._closing:
            return
        bundle = None
        try:
            from tryaii.catalog import client as catalog_client

            bundle = catalog_client.select_catalog("auto").bundle
        except Exception:  # noqa: BLE001 - starter catalog is the fallback
            logger.debug("catalog selection failed; using the starter catalog", exc_info=True)
        if bundle is None:
            from tryaii.catalog.bundle import starter_bundle

            bundle = starter_bundle()
        self._set_bundle(bundle)
        from tryaii import daemon as daemon_mod

        if daemon_mod.is_disabled():
            return
        if not self._embedding_model_ready():
            return
        try:
            self._state = daemon_mod.ensure_daemon(self._config, bundle=bundle)
        except Exception:  # noqa: BLE001 - never let daemon issues break the app
            logger.debug("daemon unavailable", exc_info=True)
            self._state = None
        self._state_checked_at = time.monotonic()

    def _embedding_model_ready(self) -> bool:
        """Make sure the daemon will have its embedding model: for an ONNX
        model, a verified file (``TRYAII_EMBEDDING_MODEL_PATH``, else the
        installed ``tryaii-minilm-int8`` package), checked here on the worker,
        never on the hot path. Without it no daemon is started and the wrapper
        behaves exactly as without a daemon: metadata-only events, the model
        in your code is sent. The warning is logged at most once per machine
        per day, and ``tryaii monitor status`` shows the state (TS-252)."""
        from tryaii.embeddings import model_store

        model = self._config.embedding_model
        if not model_store.is_onnx_model(model):
            return True
        try:
            path = model_store.resolve_model_path(model)
        except Exception:  # noqa: BLE001 - fail-open
            logger.debug("monitor: embedding model resolution failed", exc_info=True)
            path = None
        if path is not None:
            model_store.clear_unavailable(self._config.data_dir, log=logger)
            return True
        reason = model_store.last_error() or "not found"
        model_store.report_unavailable(
            model, reason, data_dir=self._config.data_dir, log=logger,
            message=(f"tryaii monitor: embedding model {model} is not available ({reason}); "
                     f"recording metadata only and sending the model in your code. "
                     f"Fix: pip install --force-reinstall "
                     f"{model_store.MODELS[model].distribution}, or set "
                     f"{model_store.ENV_MODEL_PATH} to a copy of the model file. "
                     f"Shown at most once a day; see: tryaii monitor status"))
        return False

    # -- routing ---------------------------------------------------------

    def _daemon_state(self, probe: bool) -> Optional[dict]:
        """The live daemon's state. ``probe`` (worker only) re-discovers a
        daemon at most every 5 s; the hot path never waits for one."""
        if self._state is not None:
            return self._state
        if not probe:
            return None
        now = time.monotonic()
        if now - self._state_checked_at < STATE_RECHECK_S:
            return None
        self._state_checked_at = now
        from tryaii import daemon as daemon_mod

        try:
            key = daemon_mod.catalog_key(self._bundle) if self._bundle is not None else None
            self._state = daemon_mod._live_state(self._config, key)
        except Exception:  # noqa: BLE001
            self._state = None
        return self._state

    def route(self, text: str, *, include_embedding: bool, timeout: Optional[float] = None,
              in_path: bool = False, allowed_models: Optional[list] = None) -> RouteOutcome:
        """Route ``text`` and return the outcome; never raises.

        ``allowed_models`` (None = every model) limits the ranking: sent as
        the daemon's ``allowedModels`` when it advertises the feature, and
        ALWAYS applied to the returned ranking too (older daemons and the
        injected ``route_fn`` return everything)."""
        outcome = self._route(text, include_embedding=include_embedding, timeout=timeout,
                              in_path=in_path, allowed_models=allowed_models)
        if allowed_models is not None and outcome.ok:
            from tryaii.monitor.winner import filter_ranking

            outcome.scores = filter_ranking(outcome.scores, allowed_models)
        return outcome

    def _route(self, text: str, *, include_embedding: bool, timeout: Optional[float],
               in_path: bool, allowed_models: Optional[list]) -> RouteOutcome:
        if not text:
            return RouteOutcome(STATUS_SKIPPED)
        if self._route_fn is not None:
            return self._route_injected(text)
        from tryaii import daemon as daemon_mod

        if daemon_mod.is_disabled():
            return RouteOutcome(STATUS_DISABLED)
        if self._closing:
            return RouteOutcome(STATUS_UNAVAILABLE)
        state = self._daemon_state(probe=not in_path)
        if state is None:
            return RouteOutcome(STATUS_UNAVAILABLE)
        payload: dict = {
            "cmd": "route",
            "prompt": text,
            "priorities": DEFAULT_PRIORITIES.to_dict(),
            "topK": TOP_K,
        }
        # TS-170: the policy (and, in-path, reachability) filter the ranking
        # in the daemon; route() re-applies the list client-side as well.
        if allowed_models is not None and daemon_mod.supports(
                state, daemon_mod.FEATURE_ALLOWED_MODELS):
            payload["allowedModels"] = list(allowed_models)
        if include_embedding and daemon_mod.supports(state, daemon_mod.FEATURE_ROUTE_EMBEDDING):
            payload["includeEmbedding"] = True
        if timeout is not None:
            budget = timeout
        else:
            budget = in_path_timeout_s() if in_path else SHADOW_TIMEOUT_S
        try:
            resp = daemon_mod._request(state, payload, timeout=budget)
        except (socket.timeout, TimeoutError):
            # Gone or wedged: forget it so the next hot-path call does not
            # wait the whole budget again; the worker re-probes within 5 s.
            self._state = None
            return RouteOutcome(STATUS_TIMEOUT)
        except (OSError, ValueError):
            self._state = None  # gone or restarted: rediscover from the worker
            return RouteOutcome(STATUS_UNAVAILABLE)
        if not isinstance(resp, dict) or not resp.get("ok"):
            return RouteOutcome(STATUS_UNAVAILABLE)
        try:
            data = resp["result"]
            result = daemon_mod._deserialize_route_result(data)
        except Exception:  # noqa: BLE001 - a malformed reply counts as unavailable
            return RouteOutcome(STATUS_UNAVAILABLE)
        embedding = data.get("embedding") if isinstance(data, dict) else None
        return RouteOutcome(STATUS_OK, list(result.scores), result.classification,
                            embedding if isinstance(embedding, dict) else None)

    def _route_injected(self, text: str) -> RouteOutcome:
        try:
            scores, classification, embedding = self._route_fn(text)
        except TimeoutError:
            return RouteOutcome(STATUS_TIMEOUT)
        except Exception:  # noqa: BLE001
            return RouteOutcome(STATUS_UNAVAILABLE)
        return RouteOutcome(STATUS_OK, list(scores or []), classification,
                            embedding if isinstance(embedding, dict) else None)


_default: Optional[Transport] = None
_default_lock = threading.Lock()


def get_transport() -> Transport:
    """The process-wide transport (created and started on first use)."""
    global _default
    with _default_lock:
        if _default is None:
            _default = Transport()
    _default.start()
    return _default
