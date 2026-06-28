"""Daemon round-trip tests.

These avoid loading the real embedding model by injecting a fake router into
the server, so they exercise the socket protocol, serialization, auth, and
lifecycle without the multi-second model load.
"""

from __future__ import annotations

import threading
import time

import pytest

from tryaii import daemon as daemon_mod
from tryaii import server
from tryaii.classifiers.base import ClassificationResult
from tryaii.config import TryaiiDreConfig
from tryaii.router import RouteResult
from tryaii.scoring.engine import ModelScore
from tryaii.scoring.priorities import Priorities


def _make_result(priorities: Priorities) -> RouteResult:
    return RouteResult(
        best_model="gpt-5.2",
        scores=[
            ModelScore(
                model_id="gpt-5.2",
                final_score=0.91,
                quality_score=0.8,
                cost_score=0.5,
                speed_score=0.7,
                quality_contribution=0.4,
                cost_contribution=0.2,
                speed_contribution=0.31,
                top_benchmarks=[("MMLU", 0.77), ("ARC", 0.66)],
                reasoning="strong on reasoning",
            )
        ],
        classification=ClassificationResult(
            benchmark_scores={"MMLU": 0.77, "ARC": 0.66},
            broad_category="REASONING",
            subcategory="math",
            confidence=0.42,
            classifier_used="embedding",
            cache_hit=False,
            processing_time_ms=1.5,
            difficulty=0.3,
        ),
        priorities=priorities,
    )


class _FakeRouter:
    """Stand-in router that returns a fixed result without loading a model."""

    def __init__(self):
        self.calls: list[tuple[str, int]] = []

    def route(self, prompt, priorities=None, top_k=5):
        self.calls.append((prompt, top_k))
        return _make_result(priorities or Priorities())


@pytest.fixture()
def running_daemon(tmp_path):
    config = TryaiiDreConfig(data_dir=tmp_path)
    router = _FakeRouter()
    thread = threading.Thread(
        target=server.serve,
        kwargs=dict(config=config, idle_timeout=0, router=router, log=lambda _m: None),
        daemon=True,
    )
    thread.start()

    state = None
    for _ in range(100):
        state = daemon_mod._live_state(config)
        if state:
            break
        time.sleep(0.05)
    assert state is not None, "daemon never became ready"

    yield config, state, router

    daemon_mod.stop(config)
    thread.join(timeout=5)


def test_ping_reports_runtime_and_model(running_daemon):
    config, _state, _router = running_daemon
    info = daemon_mod.status(config)
    assert info is not None
    assert info["ok"] is True
    assert info["runtime"] == "python"
    assert info["embeddingModel"] == config.embedding_model


def test_route_round_trip_matches_in_process(running_daemon):
    _config, state, _router = running_daemon
    priorities = Priorities(quality=5, cost=1, speed=1)
    got = daemon_mod.route(state, "what's greater 5 or 5.5?", priorities, 5)
    expected = _make_result(priorities)

    assert got.best_model == expected.best_model
    score, exp_score = got.scores[0], expected.scores[0]
    assert score.model_id == exp_score.model_id
    assert score.final_score == exp_score.final_score
    assert score.top_benchmarks == exp_score.top_benchmarks
    assert got.classification.broad_category == "REASONING"
    assert got.classification.difficulty == 0.3
    assert got.priorities.quality == 5 and got.priorities.cost == 1


def test_unknown_runtime_state_is_ignored(tmp_path):
    config = TryaiiDreConfig(data_dir=tmp_path)
    daemon_mod.write_state(
        config,
        {"runtime": "node", "embeddingModel": config.embedding_model, "host": "127.0.0.1",
         "port": 1, "token": "x", "pid": 0},
    )
    # A node state file must never be treated as a usable python daemon.
    assert daemon_mod._live_state(config) is None


def test_bad_token_is_unauthorized(running_daemon):
    _config, state, _router = running_daemon
    resp = daemon_mod._request({**state, "token": "wrong-token"}, {"cmd": "ping"}, timeout=5)
    assert resp["ok"] is False
    assert resp["error"] == "unauthorized"


def test_stop_then_status_is_none(running_daemon):
    config, _state, _router = running_daemon
    assert daemon_mod.stop(config) is True
    time.sleep(0.2)
    assert daemon_mod.status(config) is None
