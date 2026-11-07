#!/usr/bin/env python
"""Regenerate shared/router/h4_cases.json: the H4 router's outputs on a few
self-written prompts (English, Hebrew, mixed, edge cases), from the Python
SDK (the reference implementation) and the installed tryaii-router-h4.

Both SDKs check against it: the token ids (after the char cap, the 700-token
cap and dropping the unknown token), every class probability (softmax of the
logits / 2, rounded to 6 decimals) and the top-3 benchmarks; and the same
after the catalog mask -- only the classes in ``mask.keep`` (the packaged
starter catalog's benchmarks when the file was made), renormalised to sum to
1. Prompts are written for this file; never put dataset text here.

Usage:
  python scripts/gen-h4-fixtures.py          # rewrite
  python scripts/gen-h4-fixtures.py --check  # exit 1 on drift
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "shared" / "router" / "h4_cases.json"
sys.path.insert(0, str(REPO / "packages" / "python"))

PROMPTS = [
    ("en-code", "Write a Python function that merges two sorted lists in linear time."),
    ("en-math", "Find all real x such that x^2 - 5x + 6 = 0, and explain each step."),
    ("en-chat", "hi! can you suggest a nice name for my new puppy?"),
    ("en-legal", "Does a verbal agreement to sell a house count as a binding contract?"),
    ("en-agent", "Open the repo, run the failing unit tests, and fix the bug in the parser."),
    ("he-code", "כתוב פונקציה בפייתון שממיינת רשימה של מספרים בסדר יורד."),
    ("he-math", "פתור את המשוואה 3x + 7 = 22 והסבר את הדרך."),
    ("he-chat", "מה דעתך על שם לחתול כתום וחמוד?"),
    ("mixed", "Translate to English: 'אני רוצה להזמין שולחן לשניים בערב'."),
    ("whitespace-and-controls", "  Tabs\tnew\nlines\r\n and\u200b zero\u00a0width   spaces  "),
    ("fullwidth-nfkc", "ＡＢＣ １２３ ﬁle ①"),
    ("emoji-only", "\U0001f600\U0001f680"),
    ("empty", ""),
    ("long-repeat", "Explain recursion with an example. " * 200),
]


def build() -> dict:
    from tryaii.catalog import resolve_bundle
    from tryaii.classifiers.head import RouterHead
    from tryaii.embeddings.static_provider import StaticEmbeddingProvider

    starter = resolve_bundle(None, catalog="starter")

    provider = StaticEmbeddingProvider()
    directory = provider.ensure_loaded()
    head = RouterHead.from_dir(directory)
    meta = provider.meta
    keep = [c for c in head.classes if c in set(starter.benchmark_names)]
    cases = []
    for name, text in PROMPTS:
        ids = provider.token_ids(text)
        p = head.probabilities(provider.embed(text))
        order = sorted(range(len(p)), key=lambda i: (-p[i], head.classes[i]))
        masked = head.scores(provider.embed(text), keep)
        morder = sorted(masked, key=lambda n: (-masked[n], n))
        cases.append({
            "name": name,
            "text": text,
            "expected": {
                "token_ids": ids,
                "probabilities": {c: round(float(v), 6) for c, v in zip(head.classes, p)},
                "top3": [head.classes[i] for i in order[:3]],
                "masked": {
                    "probabilities": {c: round(v, 6) for c, v in masked.items()},
                    "top3": morder[:3],
                    "masked_classes": len(head.classes) - len(masked),
                },
            },
        })
    return {
        "suite": "h4",
        "router_id": meta["router_id"],
        "payload_version": meta["version"],
        "normalizer": meta["normalizer"],
        "tolerance": 1e-6,
        "mask": {"catalog": f"{starter.kind}-{starter.version}", "keep": keep},
        "cases": cases,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    text = json.dumps(build(), ensure_ascii=False, indent=1) + "\n"
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(f"{OUT} is stale; run scripts/gen-h4-fixtures.py")
            return 1
        print("ok")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(text.encode("utf-8"))
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
