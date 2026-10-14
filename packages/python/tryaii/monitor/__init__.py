"""tryaii.monitor — the LLM-call wrapper's pure core (shared/monitor/SPEC.md).

Slice 1 pure API (clock-free, routing-free, fixture-frozen): text
extraction, provider class / reachability / winner selection, pins, config
precedence, status aggregation and the local store. The openai wrapper and
the daemon transport (``wrap``, ``install``, ``process``) are built on top
of these in ``tryaii.monitor.wrapper_openai`` / ``tryaii.monitor.transport``.

Slice 2 adds the ``managed`` mode, policy-aware reachability, the remote
config long poll (``tryaii.monitor.remote``) and the background uploader
(``tryaii.monitor.upload``) -- shared/monitor/API.md.
"""

from tryaii.monitor.config import (
    CONFIG_SCHEMA,
    empty_config,
    resolve_app,
    resolve_mode,
    resolve_process,
    set_process_mode,
)
from tryaii.monitor.context import current_process, process
from tryaii.monitor.events import (
    EVENT_SCHEMA,
    VECTOR_SCHEMA,
    build_event,
    build_vector,
    estimate_cost,
    format_ts,
    parse_ts,
)
from tryaii.monitor.extract import routing_text
from tryaii.monitor.pins import PINS_SCHEMA, evaluate_pin, pin_from_first_call, set_pin
from tryaii.monitor.status import STATUS_SCHEMA, aggregate
from tryaii.monitor.winner import (
    MODES,
    PROVIDERS,
    allowed_models,
    filter_ranking,
    managed_target,
    native_model_id,
    pick_winner,
    policy_allowed,
    provider_class,
    reachable,
    resolve_anchor,
    route_allowed,
    routed_model,
    shadow_allowed,
)

#: Version of the data notice the developer accepted by setting
#: ``TRYAII_API_KEY`` (sent as ``notice_version`` with every upload batch;
#: docs/sdk/monitor.md "What is uploaded").
NOTICE_VERSION = "2026-10"

__all__ = [
    "NOTICE_VERSION",
    "CONFIG_SCHEMA", "EVENT_SCHEMA", "VECTOR_SCHEMA", "PINS_SCHEMA", "STATUS_SCHEMA",
    "MODES", "PROVIDERS",
    "routing_text", "provider_class", "resolve_anchor", "reachable", "allowed_models",
    "native_model_id", "pick_winner", "routed_model", "filter_ranking", "managed_target",
    "policy_allowed", "route_allowed", "shadow_allowed",
    "build_event", "build_vector", "estimate_cost", "format_ts", "parse_ts",
    "evaluate_pin", "pin_from_first_call", "set_pin",
    "resolve_mode", "resolve_app", "resolve_process", "set_process_mode", "empty_config",
    "aggregate",
    "wrap", "install", "process", "current_process", "Monitor",
]

# The wrapper imports the pure modules above; importing it last keeps the
# package import order simple for both the CLI and the library.
from tryaii.monitor.wrapper_openai import Monitor, install, wrap  # noqa: E402
