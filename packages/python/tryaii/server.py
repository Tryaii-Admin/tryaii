"""
Server side of the routing daemon (see docs/daemon.md).

This module owns the heavy lifting: it builds a Router, warms the embedding
model once, then serves routing requests over a loopback socket until it is
idle for too long or asked to shut down. It is imported only by the daemon
process (via `tryaii serve`), never on the client's hot path.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import signal
import socket
import time
from typing import Callable, Optional

from tryaii import daemon as _daemon

logger = logging.getLogger("tryaii")


# ---------------------------------------------------------------------------
# Serialization (mirrors daemon._deserialize_route_result and the Node SDK)
# ---------------------------------------------------------------------------

def _serialize_score(score) -> dict:
    return {
        "modelId": score.model_id,
        "finalScore": score.final_score,
        "qualityScore": score.quality_score,
        "costScore": score.cost_score,
        "speedScore": score.speed_score,
        "qualityContribution": score.quality_contribution,
        "costContribution": score.cost_contribution,
        "speedContribution": score.speed_contribution,
        "topBenchmarks": [[name, value] for name, value in score.top_benchmarks],
        "reasoning": score.reasoning,
    }


def _serialize_classification(classification) -> Optional[dict]:
    if classification is None:
        return None
    return {
        "benchmarkScores": dict(classification.benchmark_scores),
        "broadCategory": classification.broad_category,
        "subcategory": classification.subcategory,
        "confidence": classification.confidence,
        "classifierUsed": classification.classifier_used,
        "cacheHit": classification.cache_hit,
        "processingTimeMs": classification.processing_time_ms,
        "difficulty": classification.difficulty,
    }


def serialize_route_result(result) -> dict:
    return {
        "bestModel": result.best_model,
        "scores": [_serialize_score(s) for s in result.scores],
        "classification": _serialize_classification(result.classification),
        "priorities": result.priorities.to_dict() if result.priorities else None,
    }


# ---------------------------------------------------------------------------
# Request handling
# ---------------------------------------------------------------------------

def _handle(req: dict, router, token: str, state: dict) -> dict:
    if req.get("token") != token:
        return {"ok": False, "error": "unauthorized"}

    cmd = req.get("cmd")
    if cmd == "ping":
        return {
            "ok": True,
            "pong": True,
            "runtime": state["runtime"],
            "version": state["version"],
            "embeddingModel": state["embeddingModel"],
            "pid": state["pid"],
            "uptimeMs": int(time.time() * 1000) - state["startedAtMs"],
        }
    if cmd == "shutdown":
        return {"ok": True, "bye": True}
    if cmd == "route":
        from tryaii.scoring.priorities import Priorities

        prompt = req.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            return {"ok": False, "error": "prompt must be a non-empty string"}
        pr = req.get("priorities") or {}
        priorities = Priorities(
            quality=pr.get("quality", 3),
            cost=pr.get("cost", 3),
            speed=pr.get("speed", 3),
        )
        top_k = int(req.get("topK", 5))
        result = router.route(prompt, priorities=priorities, top_k=top_k)
        return {"ok": True, "result": serialize_route_result(result)}

    return {"ok": False, "error": f"unknown command: {cmd}"}


# ---------------------------------------------------------------------------
# Serve loop
# ---------------------------------------------------------------------------

def serve(
    config=None,
    idle_timeout: Optional[int] = None,
    router=None,
    log: Optional[Callable[[str], None]] = None,
) -> None:
    """Run the routing daemon until idle or shut down.

    Args:
        config: TryaiiDreConfig; defaults to environment-derived config.
        idle_timeout: Seconds of inactivity before self-shutdown (env default).
            0 disables the timeout.
        router: Pre-built router (used by tests to skip the model load). When
            None, a Router is built from config and warmed.
        log: Optional line logger; defaults to stdout.
    """
    from tryaii import __version__
    from tryaii.config import TryaiiDreConfig

    config = config or TryaiiDreConfig()
    config.ensure_dirs()
    idle = idle_timeout if idle_timeout is not None else _daemon.idle_seconds()
    emit = log or (lambda msg: print(msg, flush=True))

    if router is None:
        from tryaii import Priorities, Router

        router = Router(config=config)
        emit(f"[daemon] loading embedding model '{config.embedding_model}' (one-time)...")
        started = time.time()
        router.route("warmup", priorities=Priorities(), top_k=1)
        emit(f"[daemon] model warm in {time.time() - started:.1f}s")

    srv = socket.create_server(("127.0.0.1", 0))
    host, port = srv.getsockname()[:2]
    token = secrets.token_hex(16)
    state = {
        "runtime": _daemon.RUNTIME,
        "version": __version__,
        "embeddingModel": config.embedding_model,
        "host": host,
        "port": port,
        "token": token,
        "pid": os.getpid(),
        "startedAtMs": int(time.time() * 1000),
    }
    # Write the state file only now that the model is warm: its presence is the
    # readiness signal clients poll for.
    _daemon.write_state(config, state)
    emit(
        f"[daemon] listening on {host}:{port} (pid {os.getpid()}); "
        f"idle timeout {idle}s. Stop with: tryaii daemon stop"
    )

    should_stop = {"flag": False}

    def _on_signal(*_):
        should_stop["flag"] = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass

    srv.settimeout(idle if idle and idle > 0 else None)
    try:
        while not should_stop["flag"]:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                emit("[daemon] idle timeout reached; shutting down")
                break
            except OSError:
                break
            with conn:
                try:
                    conn.settimeout(30)
                    line = _daemon.recv_line(conn)
                    if not line:
                        continue
                    req = json.loads(line)
                    resp = _handle(req, router, token, state)
                    conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
                    if req.get("cmd") == "shutdown" and resp.get("ok"):
                        emit("[daemon] shutdown requested")
                        break
                except Exception as exc:  # noqa: BLE001 -- report to client, keep serving
                    try:
                        conn.sendall(
                            (json.dumps({"ok": False, "error": str(exc)}) + "\n").encode("utf-8")
                        )
                    except OSError:
                        pass
    finally:
        srv.close()
        # Only clear the state file if it still points at us (avoid clobbering a
        # newer daemon that replaced ours).
        current = _daemon.read_state(config)
        if current and current.get("port") == port and current.get("token") == token:
            _daemon.clear_state(config)
        emit("[daemon] stopped")
