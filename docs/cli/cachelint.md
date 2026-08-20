# `tryaii cachelint` — pre-flight prompt-cache analysis

Analyze prompts **before** they are sent: will this request hit the provider's
prompt cache, and if not, what exactly breaks it? Runs entirely locally —
nothing is called, no API key is read.

```bash
tryaii cachelint request.json                 # human report
tryaii cachelint requests.json --json         # full machine-readable result
cat prompt.txt | tryaii cachelint - --provider anthropic --model claude-fable-5
```

## What it checks

1. **Dynamic content — 18 detectors.** Timestamps, epoch values, UUIDs,
   session/request IDs, secrets (`high`); unrendered template slots
   (`{name}`, `{{ jinja }}`, `${VAR}`, `%(name)s`, `<PLACEHOLDER>`,
   `[INSERT ...]`) and date literals (`medium`); clock times, bare `%s`,
   long hex IDs (`low` — advisory only).
2. **Position, not just presence.** Caching is a *prefix* match, so each
   finding's offset matters: the report computes the **stable prefix** (tokens
   before the first `high`/`medium` finding) and the impact of fixing just the
   first one.
3. **Structure.** Canonical render order (tools → system → messages) with
   per-section token counts; findings inside the system prompt are called out,
   and the classic cached unit (tools+system) is measured against the floor.
4. **Provider rules.** Per-model token floors for 7 providers (OpenAI,
   Anthropic, Gemini, xAI, OpenRouter, Bedrock, Vertex), enablement mode
   (automatic vs `cache_control`/`cachePoint` opt-in), TTLs, and gateway
   upstream inheritance (OpenRouter/Vertex → Anthropic tiers).
5. **Verdict + actionables.** One of six codes — `CACHEABLE`,
   `CACHEABLE_WITH_ACTION`, `CACHEABLE_PREFIX`, `EFFECTIVELY_UNCACHEABLE`,
   `BELOW_THRESHOLD`, `UNKNOWN_THRESHOLD` — plus ordered recommendations
   ending with the exact `usage` field to assert after deploying.

For a **list** of requests it additionally predicts per-transition outcomes:
`HIT` / `PARTIAL` / `MISS` / `AT_RISK` / `UNKNOWN`, with divergence forensics
(offset, section, likely cause such as `timestamp-datetime value changed`) and
TTL-gap checks via the optional per-item `sent_at` (strict ISO-8601).

## Input

**JSON mode** (default) — the file (or stdin via `-`) contains:

```json
{
  "prompt": {"system": "...", "messages": [{"role": "user", "content": "..."}], "tools": []},
  "llm": {"provider": "anthropic", "name": "claude-fable-5"},
  "sent_at": "2026-07-30T09:00:00Z"
}
```

`prompt` may also be a bare string. A JSON **array** of such objects (or
`{"inputs": [...]}`) enables sequence analysis. `sent_at` is optional.

**Raw-text mode** — with `--provider`, the entire input is treated as ONE raw
prompt string (never parsed as JSON): paste anything.

## Options

| Flag | Effect |
|---|---|
| `--provider <name>` | Raw-text mode for this provider (aliases accepted: `claude`→anthropic, `aws`→bedrock, `google`→gemini, ...) |
| `--model <name>` | Model for raw-text mode (requires `--provider`; omit for the provider's conservative default floor) |
| `--json` | Emit the full machine-readable result instead of the text report |

## Exit codes

- `0` — analysis completed. **Findings do not change the exit code** —
  cachelint warns, it never blocks.
- `1` — runtime failure (file not found; missing tokenizer — see below).
- `2` — usage error (missing operand, invalid JSON, invalid input shape,
  unknown provider, `--model` without `--provider`).

## Exact token counts

OpenAI/xAI paths use the o200k tokenizer for exact counts. The Node SDK
bundles it (`js-tiktoken`); on Python install the extra:

```bash
pip install tryaii[cachelint]
```

Without it, analyzing OpenAI/xAI inputs fails with install instructions
(heuristic-only providers — Anthropic, Gemini, Bedrock, Vertex — never need
it). All other counts are labeled estimates (`estimate(~3.5 chars/tok)`);
Claude counts are *deliberately* not tiktoken-based (it undercounts 15-20%
for Claude) — use `POST /v1/messages/count_tokens` for exact numbers.

## Parity

Both SDKs implement the same frozen behavior contract
(`shared/cachelint/SPEC.md`): 148 shared golden fixtures conformance-tested in
both languages, plus a cross-CLI test asserting **byte-identical stdout** for
every CLI fixture. The provider knowledge base is data
(`shared/cachelint/providers.json`), synced into both packages and
byte-compared by `test_parity.py`. Thresholds and prices are time-sensitive —
verify against live provider docs before quoting numbers.
