"""
End-to-end: JSON extraction routed through tryaii_dre.

Same task as extract_direct.py, but instead of hard-coding a model we let
TryAii-DRE pick the best one for the prompt, then dispatch via OpenRouter.

Setup:
    pip install tryaii-dre[openrouter]
    # Put your key in example/.env:  OPENROUTER_API_KEY=sk-or-...

Run:
    python extract_with_router.py
"""

from __future__ import annotations

import json
import os
import re
import sys

import time

from _env import load_env
from _logging import setup_logging

load_env()
log = setup_logging()

from tryaii_dre import Router
from tryaii_dre.integrations import OpenRouterIntegration

UNSTRUCTURED_TEXT = """
Invoice #A-2048 issued on 2026-04-12 to Acme Robotics Ltd.
Billing contact: Dana Reyes, dana@acme-robotics.io, +1 415-555-0199.
Line items:
  - 4x Servo Motor MX-28 @ $89.50 each
  - 2x Control Board v3 @ $240.00 each
  - 1x Wiring harness kit @ $64.25
Shipping: $35.00. Tax: 8.75%. Payment terms: Net 30.
"""

EXTRACTION_SCHEMA_HINT = """
Return ONLY a JSON object with this exact shape, no prose:
{
  "invoice_number": string,
  "issue_date": "YYYY-MM-DD",
  "customer": {"name": string, "contact_name": string, "email": string, "phone": string},
  "line_items": [{"description": string, "quantity": integer, "unit_price": number}],
  "shipping": number,
  "tax_rate_percent": number,
  "payment_terms": string
}
""".strip()

SYSTEM_MESSAGE = "You are a precise data extraction engine. Output valid JSON only."


def extract_json_block(text: str) -> str:
    """Strip fenced code blocks if the model wrapped the JSON."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1)
    return text.strip()


def main() -> int:
    log.info("=== extract_with_router: start ===")
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        log.error("OPENROUTER_API_KEY not set -- add it to example/.env")
        return 1
    log.info("Loaded OPENROUTER_API_KEY (prefix=%s..., len=%d)", api_key[:10], len(api_key))

    prompt = (
        f"Extract structured data from this invoice text.\n\n"
        f"TEXT:\n{UNSTRUCTURED_TEXT}\n\n"
        f"{EXTRACTION_SCHEMA_HINT}"
    )
    log.info("Built prompt: %d chars, %d lines", len(prompt), prompt.count("\n") + 1)

    log.info("Initializing tryaii_dre.Router (loads centroids, embedding provider)")
    t0 = time.perf_counter()
    router = Router()
    log.info("Router ready in %.1f ms", (time.perf_counter() - t0) * 1000)

    priorities = {"quality": 5, "cost": 2, "speed": 3}
    log.info("Routing priorities: %s", priorities)

    log.info("Peeking at router.route() before dispatch to surface scores")
    from tryaii_dre.scoring.priorities import Priorities
    route_result = router.route(prompt, priorities=Priorities.from_dict(priorities))
    log.info("Router picked: %s", route_result.best_model)
    for i, s in enumerate(route_result.scores[:3], 1):
        log.info(
            "  top-%d %-40s score=%.4f | %s",
            i,
            getattr(s, "model_id", getattr(s, "model", "?")),
            getattr(s, "total", getattr(s, "score", 0.0)),
            getattr(s, "reasoning", ""),
        )

    with OpenRouterIntegration(router, app_name="tryaii-dre-example") as openrouter:
        log.info("Dispatching chat() via OpenRouter (quality-leaning)")
        t1 = time.perf_counter()
        response = openrouter.chat(
            prompt,
            system_message=SYSTEM_MESSAGE,
            priorities=priorities,
            temperature=0,
        )
        log.info(
            "chat() completed in %.1f ms -- model=%s slug=%s",
            (time.perf_counter() - t1) * 1000,
            response.model_used,
            response.openrouter_model,
        )

    usage = response.usage
    log.info(
        "Usage -- prompt=%s completion=%s total=%s",
        usage.get("prompt_tokens", "?"),
        usage.get("completion_tokens", "?"),
        usage.get("total_tokens", "?"),
    )
    log.debug("Raw model content:\n%s", response.content)

    parsed = json.loads(extract_json_block(response.content))
    log.info("Parsed JSON successfully: %d top-level keys", len(parsed))

    print(f"\nRouted model:     {response.model_used}")
    print(f"OpenRouter slug:  {response.openrouter_model}")
    print(f"Why:              {response.route_reasoning}")
    print(f"Tokens:           {usage.get('total_tokens', '?')}")
    print("Extracted JSON:")
    print(json.dumps(parsed, indent=2))
    log.info("=== extract_with_router: done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
