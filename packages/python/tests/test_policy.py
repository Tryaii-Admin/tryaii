"""Data save: model origin / provider policies (shared/datasave/SPEC.md).

* fixtures -- the Python engine reproduces shared/datasave/fixtures/expected.json
  (the same file the Node suite checks), and the file is not stale;
* ModelPolicy -- normalization, merge, summary, validation;
* bundle -- origins.json is an optional, hash-checked data file (contract v1.2);
* Router -- filters candidates, fails closed without origin data, pins hosts;
* OpenRouter payload -- ``provider.only`` is sent exactly when hosts are constrained;
* saved policy + CLI -- ``tryaii policy``, flags overriding the saved policy;
* daemon -- allowedModels only goes to a daemon that advertises support.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from tryaii import policy as pol
from tryaii.catalog import BundleIntegrityError, bundle_from_texts, canonical_json, sha256_hex
from tryaii.catalog.bundle import BUNDLE_DATA_FILES, STARTER_BUNDLE_DIR, starter_bundle
from tryaii.classifiers.base import ClassificationResult
from tryaii.policy import ModelPolicy, PolicyCatalogError, PolicyError, evaluate

from .conftest import REPO_ROOT

FIXTURES = REPO_ROOT / "shared" / "datasave" / "fixtures"


def _fixture_input() -> dict:
    return json.loads((FIXTURES / "input.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- fixtures
def test_fixtures_are_not_stale():
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "gen-datasave-fixtures.py"), "--check"],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_engine_reproduces_expected_fixtures():
    data = _fixture_input()
    expected = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
    for case, want in zip(data["cases"], expected["cases"]):
        try:
            policy = ModelPolicy.from_dict(case["policy"])
            result = evaluate(policy, data["origins"], data["model_ids"])
        except PolicyError as exc:
            assert want.get("error") == str(exc), case["name"]
            continue
        assert "error" not in want, case["name"]
        assert result.allowed == want["allowed"], case["name"]
        assert result.excluded == want["excluded"], case["name"]
        assert policy.summary() == want["summary"]
        for mid in result.allowed:
            assert result.provider_preferences(mid) == want["provider"][mid]


# ---------------------------------------------------------------- policy object
def test_policy_normalizes_lists_and_case():
    p = ModelPolicy(origin=" US-EU ", providers="OpenAI, anthropic,openai",
                    deny_countries=["cn", "CN"], platform="Bedrock")
    assert p.origin == "us-eu" and p.platform == "bedrock"
    assert p.providers == ("anthropic", "openai")
    assert p.deny_countries == ("CN",)
    assert p.summary() == (
        "origin us-eu; providers anthropic,openai; deny-countries CN; platform bedrock")


def test_inactive_policy_allows_everything_without_origins():
    result = evaluate(ModelPolicy(), None, ["a/b", "c/d"])
    assert result.allowed == ["a/b", "c/d"] and not result.excluded
    assert result.provider_preferences("a/b") is None


def test_active_policy_without_origins_fails_closed():
    with pytest.raises(PolicyCatalogError, match="need the full catalog"):
        evaluate(ModelPolicy(origin="no-prc-models"), None, ["a/b"])


def test_merge_overrides_field_by_field():
    saved = ModelPolicy(origin="no-prc-models", providers=("openai",))
    merged = saved.merged({"providers": "anthropic", "platform": None, "origin": ""})
    assert merged.origin == "no-prc-models"
    assert merged.providers == ("anthropic",)


def test_round_trip_dict():
    p = ModelPolicy(origin="no-prc-hosting", allow_countries=("US", "FR"))
    assert ModelPolicy.from_dict(p.to_dict()) == p


def test_host_only_policy_does_not_constrain_models():
    p = ModelPolicy(origin="no-prc-hosting")
    assert p.constrains_hosts and not p.constrains_models
    p = ModelPolicy(origin="no-prc-models")
    assert p.constrains_models and not p.constrains_hosts


# ---------------------------------------------------------------- bundle
def _starter_files() -> tuple[dict, dict]:
    manifest = json.loads((STARTER_BUNDLE_DIR / "manifest.json").read_text(encoding="utf-8"))
    files = {name: (STARTER_BUNDLE_DIR / name).read_text(encoding="utf-8")
             for name in BUNDLE_DATA_FILES}
    return manifest, files


def _origins_for(model_ids: list[str]) -> dict:
    """Synthetic origins over real starter ids: every model is US except qwen*/deepseek*."""
    orgs = {
        "usco": {"name": "US Co", "country": "US", "control_country": "US", "flags": [],
                 "namespaces": []},
        "cnco": {"name": "CN Co", "country": "CN", "control_country": "CN", "flags": [],
                 "namespaces": []},
    }
    hosts = {"ushost": {"name": "US Host", "org": "usco", "headquarters": "US",
                        "datacenters": ["US"]},
             "cnhost": {"name": "CN Host", "org": "cnco", "headquarters": "CN",
                        "datacenters": ["CN"]}}
    models = {}
    for mid in model_ids:
        cn = mid.split("/")[0] in ("qwen", "deepseek", "z-ai", "moonshotai", "minimax")
        models[pol.base_model_id(mid)] = {
            "developer": "cnco" if cn else "usco", "lineage": ["cnco" if cn else "usco"],
            "known": True, "kind": "model", "open_weights": None, "license": None,
            "hosts": ["cnhost", "ushost"], "platforms": {}}
    return {"schema": 1, "as_of": None, "orgs": orgs, "hosts": hosts, "models": models}


def _bundle_with_origins():
    manifest, files = _starter_files()
    ids = [m["model_id"] for m in json.loads(files["models.json"])["models"]]
    text = canonical_json(_origins_for(ids))
    manifest = {**manifest,
                "files": {**manifest["files"], "origins.json": sha256_hex(text.encode())}}
    return bundle_from_texts(manifest, {**files, "origins.json": text}), ids


def test_origins_json_is_optional_and_parsed():
    assert starter_bundle().origins is None
    bundle, _ = _bundle_with_origins()
    assert set(bundle.origins) >= {"orgs", "hosts", "models"}


def test_listed_origins_json_must_be_present_and_match():
    manifest, files = _starter_files()
    manifest = {**manifest, "files": {**manifest["files"], "origins.json": "0" * 64}}
    with pytest.raises(BundleIntegrityError, match="missing origins.json"):
        bundle_from_texts(manifest, files)
    with pytest.raises(BundleIntegrityError, match="origins.json does not match"):
        bundle_from_texts(manifest, {**files, "origins.json": "{}"})


def test_unlisted_origins_json_is_ignored():
    manifest, files = _starter_files()
    bundle = bundle_from_texts(manifest, {**files, "origins.json": "not even json"})
    assert bundle.origins is None


def test_load_bundle_reads_listed_origins(tmp_path):
    bundle, _ = _bundle_with_origins()
    for name in BUNDLE_DATA_FILES:
        (tmp_path / name).write_bytes((STARTER_BUNDLE_DIR / name).read_bytes())
    (tmp_path / "origins.json").write_bytes(canonical_json(bundle.origins).encode("utf-8"))
    (tmp_path / "manifest.json").write_text(json.dumps(bundle.manifest), encoding="utf-8")
    from tryaii.catalog import load_bundle

    assert load_bundle(tmp_path).origins == bundle.origins


# ---------------------------------------------------------------- router
class _StubClassifier:
    def __init__(self, benchmarks):
        self._scores = {b: 1.0 for b in benchmarks}

    def classify(self, prompt):
        return ClassificationResult(benchmark_scores=self._scores, broad_category="GENERAL",
                                    subcategory="GENERAL", confidence=1.0, classifier_used="stub")


def _router(bundle, policy):
    from tryaii import Router

    router = Router(bundle=bundle, policy=policy)
    router._classifier = _StubClassifier(bundle.benchmark_names)
    return router


def test_router_without_origins_rejects_an_active_policy():
    from tryaii import Router

    with pytest.raises(PolicyCatalogError):
        Router(bundle=starter_bundle(), policy={"origin": "no-prc-models"})
    Router(bundle=starter_bundle(), policy=None)  # no policy: fine


def test_router_filters_candidates_and_pins_hosts():
    bundle, ids = _bundle_with_origins()
    router = _router(bundle, ModelPolicy(origin="no-prc-hosting", providers=None))
    assert router.provider_preferences(router.policy_result.allowed[0]) == {"only": ["ushost"]}

    router = _router(bundle, {"origin": "no-prc-models"})
    result = router.route("hello", top_k=50)
    assert result.scores
    assert not any(s.model_id.startswith(("qwen/", "deepseek/")) for s in result.scores)
    assert router.provider_preferences(result.best_model) is None  # hosts unconstrained
    excluded = next(iter(router.policy_result.excluded), None)
    if excluded is not None:
        with pytest.raises(PolicyError, match="not allowed"):
            router.provider_preferences(excluded)


def test_route_allowed_models_limits_candidates():
    bundle, ids = _bundle_with_origins()
    router = _router(bundle, None)
    result = router.route("hello", top_k=50, allowed_models=ids[:2])
    assert {s.model_id for s in result.scores} <= set(ids[:2])


def test_saved_policy_is_the_router_default(monkeypatch, tmp_path):
    monkeypatch.setenv("TRYAII_DRE_DATA_DIR", str(tmp_path))
    pol.save_policy(ModelPolicy(origin="us-eu"))
    bundle, _ = _bundle_with_origins()
    assert _router(bundle, "auto").policy.origin == "us-eu"
    assert not _router(bundle, None).policy.active


# ---------------------------------------------------------------- openrouter payload
class _Resp:
    status_code = 200

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}], "model": "x", "usage": {}}

    def raise_for_status(self):
        pass


def _capture_payload(router, monkeypatch):
    from tryaii.integrations.openrouter import OpenRouterIntegration

    integ = OpenRouterIntegration(router=router, api_key="k")
    sent = {}

    def fake_request(method, path, **kwargs):
        sent.update(kwargs["json"])
        return _Resp()

    monkeypatch.setattr(integ, "_ensure_client", lambda: None)
    monkeypatch.setattr(integ, "_request_with_retry", fake_request)
    return integ, sent


def test_chat_pins_provider_only_under_a_host_policy(monkeypatch):
    bundle, _ = _bundle_with_origins()
    router = _router(bundle, {"origin": "no-prc-hosting"})
    integ, sent = _capture_payload(router, monkeypatch)
    integ.chat(prompt="hello")
    assert sent["provider"] == {"only": ["ushost"]}


def test_chat_without_host_constraint_sends_no_provider(monkeypatch):
    bundle, _ = _bundle_with_origins()
    integ, sent = _capture_payload(_router(bundle, {"origin": "no-prc-models"}), monkeypatch)
    integ.chat(prompt="hello")
    assert "provider" not in sent
    integ, sent = _capture_payload(_router(bundle, None), monkeypatch)
    integ.chat(prompt="hello")
    assert "provider" not in sent


def test_chat_override_of_an_excluded_model_is_refused(monkeypatch):
    bundle, ids = _bundle_with_origins()
    router = _router(bundle, {"origin": "no-prc-models"})
    excluded = next(iter(router.policy_result.excluded), None)
    if excluded is None:
        pytest.skip("starter has no PRC model")
    integ, sent = _capture_payload(router, monkeypatch)
    with pytest.raises(PolicyError):
        integ.chat(prompt="hello", override_model=excluded)
    assert not sent


# ---------------------------------------------------------------- saved policy + CLI
@pytest.fixture
def data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("TRYAII_DRE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRYAII_NO_BANNER", "1")
    return tmp_path


def _cli(monkeypatch, *argv):
    from tryaii.cli import main as cli_main

    monkeypatch.setattr(sys, "argv", ["tryaii", *argv])
    cli_main.cli()


def test_policy_command_set_show_clear(data_dir, monkeypatch, capsys):
    _cli(monkeypatch, "policy")
    assert capsys.readouterr().out == "No policy saved. All models are allowed.\n"
    _cli(monkeypatch, "policy", "set", "--origin", "no-prc-models",
         "--providers", "openai,Anthropic")
    assert capsys.readouterr().out == (
        "Saved policy: origin no-prc-models; providers anthropic,openai\n")
    _cli(monkeypatch, "policy", "set", "--platform", "bedrock")
    assert capsys.readouterr().out == (
        "Saved policy: origin no-prc-models; providers anthropic,openai; platform bedrock\n")
    _cli(monkeypatch, "policy", "show")
    assert "platform bedrock" in capsys.readouterr().out
    _cli(monkeypatch, "policy", "clear")
    assert capsys.readouterr().out == "Policy cleared.\n"
    _cli(monkeypatch, "policy", "clear")
    assert capsys.readouterr().out == "No policy saved.\n"


def test_policy_set_without_flags_or_with_bad_value_exits_2(data_dir, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        _cli(monkeypatch, "policy", "set")
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        _cli(monkeypatch, "policy", "set", "--origin", "no-china")
    assert exc.value.code == 2
    assert "unknown origin" in capsys.readouterr().err
    assert not pol.policy_path().exists()


def test_models_with_policy_on_starter_fails_closed(data_dir, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        _cli(monkeypatch, "models", "--origin", "no-prc-models")
    assert exc.value.code == 1
    assert capsys.readouterr().err.strip() == pol.MSG_NEEDS_FULL_CATALOG


def test_broken_saved_policy_fails_closed(data_dir, monkeypatch, capsys):
    pol.policy_path().write_text("{nope", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        _cli(monkeypatch, "models")
    assert exc.value.code == 1
    assert "not valid JSON" in capsys.readouterr().err


def test_models_lists_origin_and_filters(data_dir, monkeypatch, capsys):
    bundle, ids = _bundle_with_origins()
    from tryaii.cli import main as cli_main

    monkeypatch.setattr(cli_main, "_cli_catalog", lambda nudge=False: bundle)
    monkeypatch.setattr(cli_main, "_write_paced", lambda text: sys.stdout.write(text))
    _cli(monkeypatch, "models")
    out = capsys.readouterr().out
    assert " | origin: US" in out
    _cli(monkeypatch, "models", "--origin", "no-prc-models", "--json")
    listed = json.loads(capsys.readouterr().out)
    assert listed and all(m["origin"] == "US" for m in listed)


# ---------------------------------------------------------------- daemon
def test_daemon_route_requires_allowed_models_feature(monkeypatch):
    from tryaii import daemon
    from tryaii.scoring.priorities import Priorities

    calls = []
    monkeypatch.setattr(daemon, "_request", lambda state, payload, timeout: calls.append(payload)
                        or {"ok": True, "result": {"bestModel": "", "scores": []}})
    with pytest.raises(RuntimeError, match="allowedModels"):
        daemon.route({}, "p", Priorities(), 1, allowed_models=["a/b"])
    assert not calls
    daemon.route({"features": [daemon.FEATURE_ALLOWED_MODELS]}, "p", Priorities(), 1,
                 allowed_models=["a/b"])
    assert calls[-1]["allowedModels"] == ["a/b"]
    daemon.route({}, "p", Priorities(), 1)
    assert "allowedModels" not in calls[-1]


def test_server_passes_allowed_models(monkeypatch):
    from tryaii import server

    seen = {}

    class R:
        def route(self, prompt, priorities, top_k, allowed_models=None):
            seen["allowed"] = allowed_models
            from tryaii.router import RouteResult
            return RouteResult(best_model="")

    resp = server._handle({"cmd": "route", "token": "t", "prompt": "p", "allowedModels": ["a/b"]},
                          R(), "t", {})
    assert resp["ok"] and seen["allowed"] == ["a/b"]
