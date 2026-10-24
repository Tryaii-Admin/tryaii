#!/usr/bin/env python
"""Regenerate the machine-generated parts of shared/monitor/fixtures/.

The Python engine (packages/python/tryaii/monitor) is the REFERENCE
implementation. This script runs each suite's target over the hand-written
inputs and rewrites, in place, every case's `expected` block in
<suite>/cases.json.

Inputs, case names, descriptions and the `corpus/` stores are never touched.
Regenerated output must be hand-reviewed before commit -- the frozen files
are the conformance contract for the TypeScript port (shared/monitor/SPEC.md
section 4). Every suite is routing-free and clock-free: scores arrive in the
daemon wire shape and `now` is an input, so no fixture ever needs the
embedding model or the daemon. Model data comes from the STARTER catalog
bundle so the fixtures never depend on a login. The `cli` and `apps` suites
run the real CLI in fresh temp dirs; `apps` answers every HTTP request from
canned responses through the TRYAII_HTTP_FIXTURE seam (SPEC.md §6.3), so no
suite ever touches the network.

Usage:
  python scripts/gen-monitor-fixtures.py                # all suites
  python scripts/gen-monitor-fixtures.py --suite winner # one suite
  python scripts/gen-monitor-fixtures.py --check        # no writes; exit 1 on drift
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "shared" / "monitor" / "fixtures"
PKG_PYTHON = REPO / "packages" / "python"

sys.path.insert(0, str(PKG_PYTHON))

SUITES = ("extract", "resolve", "winner", "pin", "config", "status", "cache", "cache_usage",
          "cli", "apps")

#: The API base every `apps` case talks to (never resolvable).
FIXTURE_API_URL = "https://api.tryaii.test"
#: credentials.json written for `"signed_in": true` apps cases (SPEC.md §7.4):
#: an access token valid until 2099, so no refresh request is ever made.
FIXTURE_CREDENTIALS = {
    "version": 1,
    "api_url": FIXTURE_API_URL,
    "user": {"id": "google-sub-1", "email": "dev@example.com", "name": "Dev Example"},
    "entitlements": ["catalog:full"],
    "refresh_token": "tair_fixture_refresh",
    "refresh_expires_at": "2099-01-01T00:00:00Z",
    "access_token": "eyJ.fixture.access",
    "access_expires_at": "2099-01-01T00:00:00Z",
    "created_at": "2026-10-12T00:00:00Z",
}

SEVEN_DAYS = 7 * 86400


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def _registry():
    from tryaii.catalog.bundle import starter_bundle
    from tryaii.registry.models import ModelRegistry

    return ModelRegistry.from_bundle(starter_bundle())


def _store_dir(name: str) -> Path:
    return FIXTURES / "corpus" / name


def _store_events(name: str, now: str, since_seconds: float) -> list:
    from tryaii.monitor import store

    return store.read_events(_store_dir(name), now, since_seconds)


def _store_pins(name: str) -> dict:
    from tryaii.monitor import store

    return store.read_pins(_store_dir(name))


def _store_config(name: str) -> dict:
    from tryaii.monitor import store

    return store.read_config(_store_dir(name))


def _store_remote(name: str):
    from tryaii.monitor import store

    return store.read_remote(_store_dir(name))


def _store_upload(name: str) -> tuple:
    from tryaii.monitor import store

    cursor = store.read_upload_cursor(_store_dir(name))
    return cursor, store.uploaded_event_count(_store_dir(name), cursor)


def _policy_allowed(inp: dict, registry):
    """The case's policy-allowed ids: `policy_allowed` (a list, or null =
    no policy) as given, or `policy` (a ModelPolicy dict) evaluated against
    the starter bundle -- which carries no origins.json, so an active
    policy fails closed to []."""
    if "policy" in inp:
        from tryaii.catalog.bundle import starter_bundle
        from tryaii.monitor.winner import policy_allowed
        from tryaii.policy import ModelPolicy

        return policy_allowed(ModelPolicy.from_dict(inp["policy"]),
                              starter_bundle().origins, registry.model_ids)
    return inp.get("policy_allowed")


# ---------------------------------------------------------------------------
# Per-suite generators: input dict -> expected
# ---------------------------------------------------------------------------

def gen_extract(inp):
    from tryaii.monitor.extract import routing_text

    text, digest = routing_text(inp.get("messages"))
    return {"text": text, "prompt_hash": digest}


def gen_cache(inp):
    """API.md v1.4 cache fields (tryaii.monitor.cache). ``kind`` picks the
    function: ``usage`` (usage_from_response), ``mask`` (mask_excerpt),
    ``section`` (section_kind over a list of engine labels),
    ``recommendation`` (recommendation_from), ``report`` (one call through
    the engine on lint_prompt(messages, tools, functions), build_report) or
    ``tracker`` (CacheTracker over a list of calls, each with its process,
    model, messages, optional tools/functions and clock ``t`` seconds; the
    output adds ``analyses``, the number of full analyses run)."""
    from tryaii.monitor import cache
    from tryaii.monitor.events import usage_from_response

    kind = inp["kind"]
    if kind == "usage":
        return {"usage": usage_from_response(inp.get("usage"))}
    if kind == "mask":
        return {"excerpt": cache.mask_excerpt(inp["text"])}
    if kind == "section":
        return {"sections": [cache.section_kind(label) for label in inp["labels"]]}
    if kind == "recommendation":
        return {"recommendation": cache.recommendation_from(inp.get("recommendations"),
                                                            inp.get("verdict"))}
    if kind == "report":
        from tryaii.cachelint.analyzer import analyze_item

        prompt = cache.lint_prompt(inp["messages"], inp.get("tools"), inp.get("functions"))
        item = analyze_item({"prompt": prompt,
                             "llm": {"provider": cache.LINT_PROVIDER, "name": inp["model"]}})
        return {"report": cache.build_report(item.data, item.canonical)}
    if kind == "tracker":
        clock = {"t": 0.0}
        tracker = cache.CacheTracker(clock=lambda: clock["t"])
        out = []
        for call in inp["calls"]:
            clock["t"] = float(call.get("t", 0))
            out.append(tracker.observe(call["process"], call["model"], call["messages"],
                                       call.get("tools"), call.get("functions")))
        return {"blocks": out, "analyses": tracker.analyses}
    raise ValueError(f"unknown cache fixture kind: {kind}")


def gen_resolve(inp):
    from tryaii.monitor.winner import (
        allowed_models,
        native_model_id,
        provider_class,
        reachable,
        resolve_anchor,
        route_allowed,
        shadow_allowed,
    )

    registry = _registry()
    provider = provider_class(inp.get("base_url"))
    anchor = resolve_anchor(inp.get("model"), registry)
    policy = _policy_allowed(inp, registry)
    ids = registry.model_ids
    return {
        "provider": provider,
        "anchor": anchor,
        "anchor_reachable": reachable(provider, anchor, anchor),
        "allowed_models": allowed_models(provider, ids, anchor),
        "native": native_model_id(provider, anchor) if anchor else None,
        "policy_allowed": policy,
        "route_allowed": route_allowed(provider, ids, anchor, policy),
        "shadow_allowed": shadow_allowed(ids, policy),
    }


def gen_winner(inp):
    """The recommendation is picked on the policy-filtered ranking; the
    in-path ranking (what the daemon returns for `route_allowed`) is the
    same ranking filtered to reachable ∩ policy, and its best-for-less model
    is what dynamic sends (SPEC.md §2.3-§2.4, §2.8-§2.9)."""
    from tryaii.monitor.winner import (
        filter_ranking,
        managed_target,
        native_model_id,
        pick_winner,
        reachable,
        route_allowed,
        routed_model,
    )

    registry = _registry()
    anchor = inp.get("anchor")
    provider = inp.get("provider", "openai")
    mode = inp.get("mode", "off")
    allowed = inp.get("allowed")
    scores = inp.get("scores") or []
    picked = pick_winner(scores, anchor, registry, allowed=allowed)
    winner = picked["winner"]
    in_path = filter_ranking(scores, route_allowed(provider, registry.model_ids, anchor, allowed))
    routed_winner = pick_winner(in_path, anchor, registry)
    status = None
    if mode == "managed":
        decision = managed_target(provider, anchor, inp.get("managed_model"), registry,
                                  allowed=allowed)
        routed, status = decision["target"], decision["route_status"]
    else:
        routed = routed_model(mode, provider, anchor, routed_winner["winner"],
                              inp.get("pin_model"), allowed=allowed)
    return {
        "winner": picked,
        "winner_reachable": reachable(provider, winner, anchor),
        "routed_winner": routed_winner,
        "routed": routed,
        "model_to_send": native_model_id(provider, routed) if routed else None,
        "route_status": status,
    }


def _pin_models(spec):
    """A pin case's ``models`` (tie-break facts): inline, or ``"starter"`` =
    model_facts() of the starter catalog; absent = None (ids only)."""
    if spec == "starter":
        from tryaii.catalog.bundle import starter_bundle
        from tryaii.monitor.pins import model_facts

        return model_facts(starter_bundle())
    return spec


def gen_pin(inp):
    from tryaii.monitor.pins import evaluate_pin, pin_from_first_call

    if "facts_of" in inp:
        facts = _pin_models("starter")
        return {mid: facts.get(mid) for mid in inp["facts_of"]}
    if "first_call_winner" in inp:
        return pin_from_first_call(inp["first_call_winner"], inp["now"])
    events = inp.get("events")
    if events is None:
        events = _store_events(inp["store"], inp["now"], SEVEN_DAYS)
    pin = inp.get("pin")
    if pin == "from_store":
        pin = _store_pins(inp["store"])["processes"].get(inp["process"])
    return evaluate_pin(events, pin, inp["now"], inp["process"], _pin_models(inp.get("models")))


def gen_config(inp):
    from tryaii.monitor.config import resolve_app, resolve_mode, resolve_process, set_process_mode

    out = {}
    if "mode" in inp:
        m = inp["mode"]
        out["mode"] = resolve_mode(m["process"], env=m.get("env"), remote=m.get("remote"),
                                   args=m.get("args"), config=m.get("config"))
    if "app" in inp:
        a = inp["app"]
        out["app"] = resolve_app(env=a.get("env"), args=a.get("args"), cwd_name=a.get("cwd_name"))
    if "process" in inp:
        p = inp["process"]
        out["process"] = resolve_process(per_call=p.get("per_call"), context=p.get("context"),
                                         default=p.get("default"), call_site=p.get("call_site"))
    if "set" in inp:
        s = inp["set"]
        try:
            out["set"] = set_process_mode(s.get("config"), s["process"], s["mode"],
                                          s.get("model"))
        except ValueError as exc:
            out["set"] = {"error": str(exc)}
    return out


def gen_status(inp):
    from tryaii.monitor.status import aggregate, window_seconds

    since = inp.get("since", "24h")
    events = _store_events(inp["store"], inp["now"], window_seconds(since))
    cursor, uploaded = _store_upload(inp["store"])
    return aggregate(inp.get("app", inp["store"]), events, _store_pins(inp["store"]),
                     _store_config(inp["store"]), inp["now"], since, env=inp.get("env"),
                     remote=_store_remote(inp["store"]), upload_cursor=cursor,
                     uploaded_events=uploaded)


def gen_cache_usage(inp):
    """The provider field map of SPEC.md §3.1b: raw usage (a dict in the
    provider's shape, or null) -> the event's cache columns."""
    from tryaii.monitor.events import normalize_cache_usage

    return normalize_cache_usage(inp.get("provider"), inp.get("raw_usage"))


#: Environment variables a `cli` case never inherits from the caller (the
#: upload opt-outs change `status` and `upload` output; SPEC.md §9; the
#: Langfuse switch adds a `status` line, SPEC.md "Langfuse export").
_CLI_SCRUBBED_ENV = ("TRYAII_MONITOR_UPLOAD", "TRYAII_TELEMETRY", "DO_NOT_TRACK", "CI",
                     "TRYAII_LANGFUSE")


def run_cli_case(inp: dict) -> subprocess.CompletedProcess:
    """Run the Python CLI for a cli-suite case in a FRESH temp cwd.

    `copy_store` names corpus stores staged into `<tmp>/monitor/<name>/`
    and TRYAII_DRE_DATA_DIR points at the temp cwd, so the CLI reads the
    staged store; the case's `env` key is merged into the environment.
    Cross-CLI byte parity of WRITTEN files is asserted separately by
    test_monitor_cli_parity.py -- the frozen goldens cover stdout/stderr.
    """
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ,
                   TRYAII_NO_BANNER="1",
                   PYTHONIOENCODING="utf-8",
                   PYTHONPATH=str(PKG_PYTHON),
                   TRYAII_DRE_DATA_DIR=tmp)
        for name in _CLI_SCRUBBED_ENV:
            env.pop(name, None)
        env.update(inp.get("env") or {})
        for name in inp.get("copy_store", []):
            shutil.copytree(_store_dir(name), Path(tmp) / "monitor" / name)
        return subprocess.run(
            [sys.executable, "-c",
             "from tryaii.cli.main import cli; cli()", *inp["argv"]],
            capture_output=True, cwd=tmp, env=env,
        )


def gen_cli(inp, case_name: str):
    proc = run_cli_case(inp)
    stdout = proc.stdout.decode("utf-8").replace("\r\n", "\n")
    stderr = proc.stderr.decode("utf-8").replace("\r\n", "\n")
    golden = case_name + ".stdout.golden.txt"
    return {"stdout_golden": golden,
            "stderr": stderr or None,
            "exit_code": proc.returncode}, {golden: stdout}


#: Environment variables an `apps` case never inherits from the caller.
_APPS_SCRUBBED_ENV = ("TRYAII_API_KEY", "TRYAII_API_URL", "TRYAII_HTTP_FIXTURE",
                      "TRYAII_HTTP_FIXTURE_LOG")


def run_apps_case(inp: dict) -> tuple:
    """Run the Python CLI for an apps-suite case; returns ``(proc,
    requests)``.

    Fresh temp data dir (TRYAII_DRE_DATA_DIR) and cwd; `signed_in: true`
    writes FIXTURE_CREDENTIALS there; `http` (a routes map, SPEC.md §6.3)
    is written to a SEPARATE temp dir and named by TRYAII_HTTP_FIXTURE, the
    request log by TRYAII_HTTP_FIXTURE_LOG; TRYAII_API_URL defaults to
    FIXTURE_API_URL; the case's `env` is merged last.
    """
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as side:
        env = {k: v for k, v in os.environ.items() if k not in _APPS_SCRUBBED_ENV}
        http_path = Path(side) / "http.json"
        log_path = Path(side) / "requests.jsonl"
        http_path.write_text(json.dumps({"routes": inp.get("http") or {}}), encoding="utf-8")
        env.update(TRYAII_NO_BANNER="1", PYTHONIOENCODING="utf-8",
                   PYTHONPATH=str(PKG_PYTHON), TRYAII_DRE_DATA_DIR=tmp,
                   TRYAII_API_URL=FIXTURE_API_URL,
                   TRYAII_HTTP_FIXTURE=str(http_path),
                   TRYAII_HTTP_FIXTURE_LOG=str(log_path))
        env.update(inp.get("env") or {})
        if inp.get("signed_in"):
            (Path(tmp) / "credentials.json").write_text(
                json.dumps(FIXTURE_CREDENTIALS, indent=2) + "\n", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-c",
             "from tryaii.cli.main import cli; cli()", *inp["argv"]],
            capture_output=True, cwd=tmp, env=env,
        )
        requests = []
        if log_path.exists():
            for line in log_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    requests.append(json.loads(line))
        return proc, requests


def gen_apps(inp, case_name: str):
    proc, requests = run_apps_case(inp)
    stdout = proc.stdout.decode("utf-8").replace("\r\n", "\n")
    stderr = proc.stderr.decode("utf-8").replace("\r\n", "\n")
    golden = case_name + ".stdout.golden.txt"
    return {"stdout_golden": golden,
            "stderr": stderr or None,
            "exit_code": proc.returncode,
            "requests": requests}, {golden: stdout}


# ---------------------------------------------------------------------------

def run_suite(name: str) -> dict:
    """Return {relative_path: new_content_str} for every file this suite owns."""
    suite_dir = FIXTURES / name
    cases_path = suite_dir / "cases.json"
    if not cases_path.exists():
        return {}
    doc = json.loads(cases_path.read_text(encoding="utf-8"))
    out_files: dict[str, str] = {}

    for case in doc["cases"]:
        if name in ("cli", "apps"):
            gen = gen_cli if name == "cli" else gen_apps
            expected, goldens = gen(case["input"], case["name"])
            case["expected"] = expected
            for fname, text in goldens.items():
                out_files[f"{name}/{fname}"] = text
        else:
            case["expected"] = globals()["gen_" + name](case["input"])

    out_files[f"{name}/cases.json"] = _dump(doc)
    return out_files


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--suite", choices=SUITES,
                        help="regenerate a single suite")
    parser.add_argument("--check", action="store_true",
                        help="compare instead of writing; exit 1 on drift")
    args = parser.parse_args(argv)

    suites = (args.suite,) if args.suite else SUITES
    drift: list[str] = []

    for name in suites:
        for rel, content in run_suite(name).items():
            path = FIXTURES / rel
            current = (path.read_text(encoding="utf-8")
                       if path.exists() else None)
            if args.check:
                if current != content:
                    drift.append(rel)
            elif current != content:
                path.write_text(content, encoding="utf-8", newline="\n")
                print(f"wrote {rel}")

    if args.check:
        if drift:
            print("DRIFT in:", *drift, sep="\n  ")
            return 1
        print("fixtures up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
