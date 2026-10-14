#!/usr/bin/env python3
"""Generate shared/datasave/fixtures/expected.json from input.json (shared/datasave/SPEC.md).

The Python policy engine (packages/python/tryaii/policy.py) is the reference:
every case in input.json is evaluated and its result written to expected.json,
which the Python and Node test suites both compare against byte for byte.

Usage:
    python scripts/gen-datasave-fixtures.py           # write expected.json
    python scripts/gen-datasave-fixtures.py --check   # exit 1 if it is stale
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "shared" / "datasave" / "fixtures"
sys.path.insert(0, str(ROOT / "packages" / "python"))

from tryaii.policy import ModelPolicy, PolicyError, evaluate, origin_label  # noqa: E402


def run_case(case: dict, origins: dict, model_ids: list[str]) -> dict:
    try:
        policy = ModelPolicy.from_dict(case["policy"])
        result = evaluate(policy, origins, model_ids)
    except PolicyError as exc:
        return {"name": case["name"], "error": str(exc)}
    return {
        "name": case["name"],
        "policy": policy.to_dict(),
        "summary": policy.summary(),
        "active": policy.active,
        "allowed": result.allowed,
        "excluded": result.excluded,
        "provider": {mid: result.provider_preferences(mid) for mid in result.allowed},
    }


def build() -> str:
    data = json.loads((FIXTURES / "input.json").read_text(encoding="utf-8"))
    origins, ids = data["origins"], data["model_ids"]
    expected = {
        "cases": [run_case(c, origins, ids) for c in data["cases"]],
        "labels": {mid: origin_label(origins, mid) for mid in data["labels"]},
        "no_origins": run_case({"name": "active policy without origins.json",
                                "policy": {"origin": "us-eu"}}, None, ids),
    }
    return json.dumps(expected, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    text = build()
    path = FIXTURES / "expected.json"
    if args.check:
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            print("shared/datasave/fixtures/expected.json is stale; run "
                  "scripts/gen-datasave-fixtures.py", file=sys.stderr)
            return 1
        print("datasave fixtures are up to date")
        return 0
    path.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
