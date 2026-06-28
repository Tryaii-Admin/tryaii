"""
Client side of the routing daemon (see docs/daemon.md).

`tryaii route`/`tryaii eval` import this module to find -- and, when needed,
auto-start -- a long-lived background process that keeps the embedding model
warm. It must stay lightweight: importing this module must NOT pull in torch /
sentence-transformers, so the heavy server lives in tryaii.server and the
dataclass imports here are deferred into the functions that need them.

The protocol and state-file format are shared byte-for-byte with the Node SDK
(packages/node/src/daemon.ts); keep the two in sync.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("tryaii")

# Identifies which SDK started a daemon. A client only reuses a daemon whose
# runtime matches, because the Python and Node embedding backends differ.
RUNTIME = "python"
PROTOCOL_VERSION = 1

DEFAULT_IDLE_SECONDS = 900
DEFAULT_WAIT_SECONDS = 180
# A spawn lock older than this is considered abandoned and may be stolen.
SPAWN_LOCK_STALE_SECONDS = 300


# ---------------------------------------------------------------------------
# Environment knobs
# ---------------------------------------------------------------------------

def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def is_disabled() -> bool:
    """True when the daemon is globally disabled via TRYAII_NO_DAEMON."""
    return _env_truthy("TRYAII_NO_DAEMON")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def idle_seconds() -> int:
    return _env_int("TRYAII_DAEMON_IDLE", DEFAULT_IDLE_SECONDS)


def wait_seconds() -> int:
    return _env_int("TRYAII_DAEMON_WAIT", DEFAULT_WAIT_SECONDS)


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------

def state_path(config) -> Path:
    return Path(config.data_dir) / f"daemon-{RUNTIME}.json"


def _lock_path(config) -> Path:
    return Path(config.data_dir) / f"daemon-{RUNTIME}.lock"


def log_path(config) -> Path:
    return Path(config.data_dir) / f"daemon-{RUNTIME}.log"


def read_state(config) -> Optional[dict]:
    try:
        return json.loads(state_path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_state(config, state: dict) -> None:
    config.ensure_dirs()
    path = state_path(config)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def clear_state(config) -> None:
    try:
        state_path(config).unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Socket request / response
# ---------------------------------------------------------------------------

def recv_line(sock: socket.socket) -> bytes:
    """Read a single newline-terminated frame from a socket."""
    buf = bytearray()
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf.extend(chunk)
        newline = buf.find(b"\n")
        if newline != -1:
            return bytes(buf[:newline])
    return bytes(buf)


def _request(state: dict, payload: dict, timeout: float) -> dict:
    """Send one request to a daemon and return its parsed response."""
    body = {**payload, "v": PROTOCOL_VERSION, "token": state["token"]}
    with socket.create_connection((state["host"], state["port"]), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall((json.dumps(body) + "\n").encode("utf-8"))
        line = recv_line(sock)
    if not line:
        raise OSError("daemon closed the connection without responding")
    return json.loads(line)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _live_state(config) -> Optional[dict]:
    """Return the state of a running daemon matching this runtime + model.

    Returns None if there is no state file, it belongs to another runtime or
    embedding model, or the process does not answer a ping.
    """
    state = read_state(config)
    if not state:
        return None
    if state.get("runtime") != RUNTIME:
        return None
    if state.get("embeddingModel") != config.embedding_model:
        return None
    try:
        resp = _request(state, {"cmd": "ping"}, timeout=5)
    except (OSError, ValueError):
        return None
    return state if resp.get("ok") else None


def status(config) -> Optional[dict]:
    """Ping the daemon and return its info, or None if not running."""
    state = read_state(config)
    if not state:
        return None
    try:
        resp = _request(state, {"cmd": "ping"}, timeout=5)
    except (OSError, ValueError):
        return None
    if not resp.get("ok"):
        return None
    info = dict(resp)
    info["host"] = state.get("host")
    info["port"] = state.get("port")
    return info


# ---------------------------------------------------------------------------
# Spawning
# ---------------------------------------------------------------------------

def _acquire_spawn_lock(config) -> bool:
    """Best-effort lock so concurrent CLI calls don't each spawn a daemon."""
    path = _lock_path(config)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        return True
    except FileExistsError:
        try:
            if time.time() - path.stat().st_mtime > SPAWN_LOCK_STALE_SECONDS:
                path.unlink()
                return _acquire_spawn_lock(config)
        except OSError:
            pass
        return False


def _release_spawn_lock(config) -> None:
    try:
        _lock_path(config).unlink()
    except OSError:
        pass


def _spawn_serve(config) -> "subprocess.Popen":
    """Launch a detached `tryaii serve` process for this config."""
    config.ensure_dirs()
    args = [sys.executable, "-m", "tryaii.cli.main", "serve", "--no-banner"]
    env = os.environ.copy()
    env["TRYAII_DRE_EMBEDDING_MODEL"] = config.embedding_model
    env["TRYAII_DRE_DATA_DIR"] = str(config.data_dir)
    # The serve process must never try to start a daemon of its own.
    env["TRYAII_NO_DAEMON"] = "1"

    logfile = open(log_path(config), "ab")  # noqa: SIM115 -- handed to the child
    kwargs: dict = dict(
        stdin=subprocess.DEVNULL,
        stdout=logfile,
        stderr=logfile,
        env=env,
        close_fds=True,
        cwd=str(config.data_dir),
    )
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(args, **kwargs)
    logfile.close()
    return proc


def ensure_daemon(
    config,
    autostart: bool = True,
    wait_timeout: Optional[float] = None,
    on_starting=None,
) -> Optional[dict]:
    """Return the state of a live daemon, starting one if needed.

    Args:
        config: TryaiiDreConfig describing the embedding model / data dir.
        autostart: Spawn a daemon if none is running.
        wait_timeout: Seconds to wait for a freshly-spawned daemon to warm up.
        on_starting: Optional zero-arg callback invoked once while waiting, so
            callers can print a "starting..." notice.

    Returns the state dict on success, or None if no daemon could be reached
    (caller should fall back to in-process routing).
    """
    info = _live_state(config)
    if info:
        return info
    if not autostart:
        return None

    # The lock and log files live in the data dir, so it must exist before we
    # try to create them (first-ever run starts from nothing).
    config.ensure_dirs()
    deadline = time.monotonic() + (wait_timeout if wait_timeout is not None else wait_seconds())
    acquired = _acquire_spawn_lock(config)
    proc = None
    try:
        if acquired:
            # Drop any stale state so we only accept the new daemon's readiness.
            clear_state(config)
            proc = _spawn_serve(config)
        notified = False
        while time.monotonic() < deadline:
            info = _live_state(config)
            if info:
                return info
            if proc is not None and proc.poll() is not None:
                logger.warning("tryaii daemon exited before becoming ready")
                return None
            if on_starting is not None and not notified:
                on_starting()
                notified = True
            time.sleep(0.25)
        return None
    finally:
        if acquired:
            _release_spawn_lock(config)


def stop(config) -> bool:
    """Stop the daemon. Returns True if one was running."""
    state = read_state(config)
    if not state:
        return False
    stopped = False
    try:
        resp = _request(state, {"cmd": "shutdown"}, timeout=5)
        stopped = bool(resp.get("ok"))
    except (OSError, ValueError):
        pass
    if not stopped and state.get("pid"):
        try:
            os.kill(int(state["pid"]), signal.SIGTERM)
            stopped = True
        except (OSError, ValueError):
            pass
    clear_state(config)
    return stopped


# ---------------------------------------------------------------------------
# Routing through the daemon
# ---------------------------------------------------------------------------

def _deserialize_route_result(data: dict):
    """Rebuild a RouteResult (and nested dataclasses) from the wire shape."""
    from tryaii.classifiers.base import ClassificationResult
    from tryaii.router import RouteResult
    from tryaii.scoring.engine import ModelScore
    from tryaii.scoring.priorities import Priorities

    scores = [
        ModelScore(
            model_id=s["modelId"],
            final_score=s["finalScore"],
            quality_score=s["qualityScore"],
            cost_score=s["costScore"],
            speed_score=s["speedScore"],
            quality_contribution=s["qualityContribution"],
            cost_contribution=s["costContribution"],
            speed_contribution=s["speedContribution"],
            top_benchmarks=[(name, value) for name, value in s.get("topBenchmarks", [])],
            reasoning=s["reasoning"],
        )
        for s in data.get("scores", [])
    ]

    classification = None
    cls = data.get("classification")
    if cls is not None:
        classification = ClassificationResult(
            benchmark_scores=dict(cls.get("benchmarkScores", {})),
            broad_category=cls.get("broadCategory", ""),
            subcategory=cls.get("subcategory", ""),
            confidence=cls.get("confidence", 0.0),
            classifier_used=cls.get("classifierUsed", ""),
            cache_hit=cls.get("cacheHit", False),
            processing_time_ms=cls.get("processingTimeMs", 0.0),
            difficulty=cls.get("difficulty", 0.0),
        )

    pr = data.get("priorities") or {}
    priorities = Priorities(
        quality=pr.get("quality", 3),
        cost=pr.get("cost", 3),
        speed=pr.get("speed", 3),
    )

    return RouteResult(
        best_model=data.get("bestModel", ""),
        scores=scores,
        classification=classification,
        priorities=priorities,
    )


def route(state: dict, prompt: str, priorities, top_k: int):
    """Route a single prompt through a running daemon. Raises on failure."""
    payload = {
        "cmd": "route",
        "prompt": prompt,
        "priorities": priorities.to_dict(),
        "topK": top_k,
    }
    resp = _request(state, payload, timeout=30)
    if not resp.get("ok"):
        raise RuntimeError(resp.get("error", "daemon route failed"))
    return _deserialize_route_result(resp["result"])
