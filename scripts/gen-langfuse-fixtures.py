#!/usr/bin/env python
"""Regenerate shared/monitor/fixtures/langfuse/cases.json (the Langfuse export).

The Python engine (packages/python/tryaii/monitor/langfuse.py) is the
REFERENCE. This script runs it over the hand-written inputs and rewrites
every case's `expected` block in place; inputs, names and descriptions are
never touched. The Node SDK (src/monitor/langfuse.ts) must reproduce every
block, and the request bodies byte for byte (`bodies_sha256`), in
packages/node/tests/monitor-langfuse.test.ts. See shared/monitor/SPEC.md,
section "Langfuse export". No case touches the network or the clock.

Case kinds (`input.kind`):
  export      {"app_id", "events": [...]} -> per-event ids, the OTLP request
              body, the score payloads and the sha256 of each exact body
  settings    {"env": {...}} -> settings(env) and status_line(env)
  exportable  {"line": {...}} -> whether the exporter sends that store line

Usage:
  python scripts/gen-langfuse-fixtures.py           # rewrite expected blocks
  python scripts/gen-langfuse-fixtures.py --check   # no writes; exit 1 on drift
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CASES = REPO / "shared" / "monitor" / "fixtures" / "langfuse" / "cases.json"
sys.path.insert(0, str(REPO / "packages" / "python"))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def gen_export(inp: dict, version: str) -> dict:
    from tryaii.monitor import langfuse as lf

    events = inp["events"]
    spans = [lf.build_span(e, inp.get("app_id")) for e in events]
    body = lf.build_traces_body(spans, version)
    scores = [lf.build_scores(e) for e in events]
    return {
        "ids": [lf.span_ids(e) for e in events],
        "tryaii_url": [lf.tryaii_url(inp.get("app_id"), e.get("process")) for e in events],
        "traces": body,
        "scores": scores,
        "bodies_sha256": {
            "traces": _sha(lf._json(body)),
            "scores": [[_sha(lf._json(s)) for s in per_event] for per_event in scores],
        },
    }


def gen_settings(inp: dict) -> dict:
    from tryaii.monitor import langfuse as lf

    env = inp["env"]
    return {"settings": lf.settings(env), "status_line": lf.status_line(env)}


def gen_exportable(inp: dict) -> bool:
    from tryaii.monitor import langfuse as lf

    return lf.exportable(inp["line"])


def gen_case(inp: dict, version: str):
    kind = inp["kind"]
    if kind == "export":
        return gen_export(inp, version)
    if kind == "settings":
        return gen_settings(inp)
    if kind == "exportable":
        return gen_exportable(inp)
    raise ValueError(f"unknown case kind: {kind}")


def render() -> str:
    doc = json.loads(CASES.read_text(encoding="utf-8"))
    for case in doc["cases"]:
        case["expected"] = gen_case(case["input"], doc["version"])
    from tryaii.monitor.store import js_numbers

    # Numbers in Node's format (SPEC.md §1.2), so the file reads as both SDKs emit.
    return js_numbers(json.dumps(doc, indent=2, ensure_ascii=False)) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="compare instead of writing; exit 1 on drift")
    args = parser.parse_args(argv)
    content = render()
    current = CASES.read_text(encoding="utf-8")
    if args.check:
        if current != content:
            print("DRIFT in: langfuse/cases.json")
            return 1
        print("fixtures up to date")
        return 0
    if current != content:
        CASES.write_text(content, encoding="utf-8", newline="\n")
        print("wrote langfuse/cases.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
