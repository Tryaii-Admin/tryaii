"""
End-to-end: direct OpenRouter API call for JSON extraction.

No routing -- we pick the model ourselves and call OpenRouter directly.
The prompt instructs the model to return strict JSON, and we parse it.

Setup:
    pip install httpx
    # Put your key in example/.env:  OPENROUTER_API_KEY=sk-or-...

Run:
    python extract_direct.py
"""

from __future__ import annotations

import json
import os
import re
import sys

import time

import httpx

from _env import load_env
from _logging import setup_logging

load_env()
log = setup_logging()

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "openai/gpt-4o-mini"

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


def extract_json_block(text: str) -> str:
    """Strip fenced code blocks if the model wrapped the JSON."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1)
    return text.strip()


def main() -> int:
    log.info("=== extract_direct: start ===")
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
    log.debug("Prompt preview (first 200 chars):\n%s", prompt[:200])

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "You are a precise data extraction engine. Output valid JSON only."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    log.info("Dispatching to %s with model=%s temperature=%s", OPENROUTER_URL, MODEL, payload["temperature"])

    t0 = time.perf_counter()
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Title": "tryaii-dre-example",
            },
            json=payload,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        log.info("HTTP %d from OpenRouter in %.1f ms", resp.status_code, elapsed_ms)
        resp.raise_for_status()
        data = resp.json()

    usage = data.get("usage", {})
    log.info(
        "Usage -- prompt=%s completion=%s total=%s (or-cost=%s)",
        usage.get("prompt_tokens", "?"),
        usage.get("completion_tokens", "?"),
        usage.get("total_tokens", "?"),
        data.get("cost", "?"),
    )

    content = data["choices"][0]["message"]["content"]
    log.debug("Raw model content:\n%s", content)

    parsed = json.loads(extract_json_block(content))
    log.info("Parsed JSON successfully: %d top-level keys", len(parsed))

    print(f"\nModel: {MODEL}")
    print(f"Tokens: {usage.get('total_tokens', '?')}")
    print("Extracted JSON:")
    print(json.dumps(parsed, indent=2))
    log.info("=== extract_direct: done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
