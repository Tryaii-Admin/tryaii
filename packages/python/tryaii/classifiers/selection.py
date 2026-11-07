"""
Router selection: H4 (default) or MiniLM centroids (fallback).

``TRYAII_ROUTER`` (``TryaiiDreConfig.router``) picks the router: ``h4``
(default) or ``minilm``. H4 applies only with the default embedding model and
no custom embedding provider; when its payload (package ``tryaii-router-h4``
or ``TRYAII_ROUTER_MODEL_PATH``) is missing or broken, routing falls back to
MiniLM by itself. The fallback is recorded in ``<data dir>/router-model.json``
and logged as a warning at most once a day (``tryaii monitor status`` shows
it); the state is dropped as soon as H4 loads again.

H4 scores only the benchmarks of the loaded catalog (the other classes are
masked and the rest renormalised, see :mod:`tryaii.classifiers.head`). A
catalog that has none of its classes cannot be routed by H4: the Router uses
MiniLM for it and logs a warning (once per process and catalog).
"""

from __future__ import annotations

import logging
from typing import Optional

from tryaii.config import TryaiiDreConfig
from tryaii.embeddings import model_store

logger = logging.getLogger("tryaii")


def fallback_message(reason: str) -> str:
    spec = model_store.ROUTERS[model_store.DEFAULT_ROUTER_MODEL]
    return (f"tryaii: router model {spec.router_id} is not available ({reason}); "
            f"routing with {model_store.DEFAULT_ONNX_MODEL} instead. "
            f"Fix: pip install --force-reinstall {spec.distribution}, or set "
            f"{model_store.ENV_ROUTER_MODEL_PATH} to a copy of its directory. "
            f"Shown at most once a day; see: tryaii monitor status")


def record_h4_unavailable(config: TryaiiDreConfig, reason: str,
                          log: Optional[logging.Logger] = None) -> None:
    """Record + (throttled) log that H4 is unavailable. Never raises."""
    try:
        model_store.report_unavailable(
            model_store.DEFAULT_ROUTER_MODEL, reason, data_dir=config.data_dir,
            log=log or logger, message=fallback_message(reason),
            state_file=model_store.ROUTER_STATE_FILE)
    except Exception:  # noqa: BLE001 - reporting must never break routing
        (log or logger).debug("could not record the router state", exc_info=True)


def record_h4_available(config: TryaiiDreConfig, log: Optional[logging.Logger] = None) -> None:
    try:
        model_store.clear_unavailable(config.data_dir, log=log or logger,
                                      state_file=model_store.ROUTER_STATE_FILE)
    except Exception:  # noqa: BLE001
        pass


def h4_ready(config: TryaiiDreConfig, log: Optional[logging.Logger] = None) -> bool:
    """Cheap check (no load): is a verified H4 payload in place? Records the
    state either way. False when H4 is not the configured router."""
    if not config.uses_h4:
        return False
    try:
        path = model_store.resolve_router_dir(model_store.DEFAULT_ROUTER_MODEL)
    except Exception:  # noqa: BLE001 - fail over to MiniLM
        path = None
    if path is None:
        record_h4_unavailable(config, model_store.last_router_error() or "not found", log)
        return False
    record_h4_available(config, log)
    return True


def load_h4_classifier(config: TryaiiDreConfig, bundle=None,
                       log: Optional[logging.Logger] = None):
    """The H4 classifier, loaded and verified, or None (state recorded) when
    the payload is missing or broken."""
    from tryaii.classifiers.head import HeadClassifier
    from tryaii.embeddings.static_provider import StaticEmbeddingProvider

    try:
        classifier = HeadClassifier(StaticEmbeddingProvider(), config=config,
                                    bundle=bundle).load()
    except Exception as exc:  # noqa: BLE001 - any failure means fallback
        reason = getattr(exc, "reason", None) or f"{type(exc).__name__}: {exc}"
        record_h4_unavailable(config, reason, log)
        return None
    record_h4_available(config, log)
    if not classifier.kept_classes:
        _warn_no_classes(bundle, log)
        return None
    return classifier


_WARNED_NO_CLASSES: set = set()


def _warn_no_classes(bundle, log: Optional[logging.Logger] = None) -> None:
    """The catalog has none of H4's benchmarks: say so (once per process
    and catalog); the Router then routes with MiniLM."""
    key = (getattr(bundle, "kind", None), getattr(bundle, "version", None))
    if key in _WARNED_NO_CLASSES:
        return
    _WARNED_NO_CLASSES.add(key)
    name = f"{key[0]}-{key[1]}" if key[0] else "the catalog"
    (log or logger).warning(
        "tryaii: none of the benchmarks the router model %s scores is in %s; "
        "routing with %s instead",
        model_store.DEFAULT_ROUTER_MODEL, name, model_store.DEFAULT_ONNX_MODEL)
