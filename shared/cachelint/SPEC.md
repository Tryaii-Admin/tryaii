# cachelint — frozen behavior spec

Status: FROZEN once the golden fixtures in `shared/cachelint/fixtures/` carry
generated `expected` blocks. Any behavior change after that point requires
editing this spec, regenerating fixtures, and updating BOTH engines in the
same PR.

cachelint is a pre-flight prompt-cache analyzer: given one or more
`{prompt, llm: {provider, name}, sent_at?}` inputs it renders a canonical
string, counts tokens, scans **18** dynamic-content detectors, computes the
stable cacheable prefix against a 7-provider knowledge base, and produces a
verdict plus recommendations. For lists it additionally predicts per-pair
cache outcomes (HIT / PARTIAL / MISS / UNKNOWN / AT_RISK).

It ships twice — `packages/python/tryaii/cachelint/` and
`packages/node/src/cachelint/` — and the two implementations must produce
**byte-identical output** for every fixture. The Python engine is the
*reference*: fixtures are generated from it (`scripts/gen-cachelint-fixtures.py`)
and the TypeScript engine conforms to the frozen files.

Provenance: the prototype at `cache_providers/cachelint` (research corpus
2026-07: `research-matrix.md`, `SYNTHESIS.md`, `providers/*.md`) plus the
deltas in §2 below. The provider knowledge base lives as data in
`shared/cachelint/providers.json` (single source of truth; synced into both
packages by `scripts/sync-shared.py`; byte-compared by `test_parity.py`).

---

## 1. Cross-language ground rules

These rules exist so that a Python `dict` → `json.dumps(..., indent=2,
ensure_ascii=False)` and a JS object → `JSON.stringify(..., null, 2)` are
byte-identical.

### 1.1 Offsets are Unicode code points

Every character offset in the engine — finding `start`/`end`,
`section.start`/`end`, `section_offset`, `lcp_chars`, `diverged_at.offset`,
excerpt padding (28), divergence context windows (−40/+100), and the length
used by the heuristic tokenizer — is measured in **Unicode code points**, not
UTF-16 code units and not bytes.

- Python: native (`str` indexes code points).
- TypeScript: regex match indices (UTF-16) must be converted through a
  code-point index map; slicing and LCP run in the code-point domain. An LCP
  must never split a surrogate pair (impossible by construction when
  comparing code point by code point).

### 1.2 Rounding is round-half-even

All rounding — `round(x)`, `round(x, 1)`, and fixed-point formatting
(`%.0f`, `%.1f`) — is IEEE round-half-to-even ("banker's rounding").

- Python: native `round()` and `format(x, '.0f')` already comply.
- TypeScript: `halfEvenRound(x, digits)` and `formatFixed(x, digits)`
  helpers; `Math.round` must NOT be used in engine code.

### 1.3 JSON integer rule

Any float in the **output tree** that is integral after rounding is emitted
as an integer: `100.0` → `100`, `600.0` → `600`, `82.0` → `82`. Applied as a
normalization pass over the result before serialization. This removes the
`1.0`-vs-`1` divergence class between `json.dumps` and `JSON.stringify`.

Non-integral floats (e.g. `82.5`) are emitted as shortest-round-trip
decimals, where Python `repr` and JS `Number#toString` agree.

Out of scope (documented limitation, not supported input): numbers that
cannot round-trip a JS double — magnitudes > 2^53 and exponent-notation
literals inside tool JSON.

### 1.4 Canonical JSON serialization (tools section)

The `tools` value is rendered into the canonical string as **minified JSON
with sorted keys**:

- separators `,` and `:` with no whitespace;
- object keys sorted by **code-point** order of the key string (Python's
  native `sort_keys`; the TS comparator compares code points explicitly —
  JS's default sort is code-unit order, which disagrees above U+FFFF);
  applied recursively;
- non-ASCII characters unescaped (UTF-8 verbatim);
- floats that are integral are emitted as integers (`1.0` → `1`), per §1.3.

Python: `json.dumps(sort_keys=True, ensure_ascii=False,
separators=(",", ":"))` after a float-normalizing pre-pass.
TypeScript: `sortedStringify` utility. Identical bytes are pinned by the
`canonical/` fixtures.

### 1.5 Thousands separators

The text report formats token counts ≥ 1000 with comma separators
(`~1,024`). Implemented with a locale-independent helper in both languages —
never `toLocaleString` / locale-sensitive formatting.

### 1.6 `sent_at` — strict ISO-8601 subset

`sent_at` values are parsed by the same regex grammar in both languages:

```
^(\d{4})-(\d{2})-(\d{2})([Tt ](\d{2}):(\d{2})(:(\d{2})(\.(\d{1,9}))?)?)?(Z|z|[+-]\d{2}:?\d{2})?$
```

- Missing time component → midnight (00:00:00).
- Missing offset (naive) → interpreted as **UTC**.
- Fractional seconds truncated to milliseconds.
- Result: epoch milliseconds (integer).
- Anything that does not match the grammar, or has out-of-range fields
  (month 13, hour 25...) → `null`; a pair with a `null` timestamp on either
  side gets **no** `ttl_check`.
- `gap_seconds = round_half_even((curr_ms - prev_ms) / 1000, 1)` — may be
  negative when requests are out of order (no `abs`, preserved behavior).

Neither engine may use its platform date parser (`datetime.fromisoformat`,
`Date.parse`) — both had version- or locale-dependent behavior.

### 1.7 Regex dialect (detectors)

Detector semantics are defined by the Python `re` module on `str`:

- `\b` / `\w` are **Unicode-aware**: a Hebrew or accented letter adjacent to
  a digit suppresses the word boundary (e.g. `של2026-01-15` does NOT match
  `date-iso`).
- TypeScript translations use the `u` flag and replace `\b` with explicit
  lookarounds `(?<![\p{L}\p{N}_])` / `(?![\p{L}\p{N}_])`; a leading `(?i)`
  becomes the `i` flag.
- Scanning is per-detector `finditer`/`matchAll` (non-overlapping,
  leftmost); results sorted by `(start, -length)` with a **stable** sort
  (ties keep detector declaration order, then match order).
- Containment dedupe: a finding is dropped only when an already-kept finding
  **strictly longer** than it fully contains it. Equal spans from different
  detectors are both kept; partial overlaps are both kept.

### 1.8 Error messages

Interpolated values use plain single quotes — no Python `repr` formatting:

- `Unknown provider 'azure'. Known: anthropic, bedrock, gemini, openai, openrouter, vertex, xai`
- `Model 'foo' not recognized for <display> — using the fallback threshold (<n>).`

CLI-level JSON parse failures carry no parser-specific detail (Python `json`
and `JSON.parse` messages differ): `invalid JSON in '<path>'` /
`invalid JSON on stdin`.

---

## 2. Deltas from the prototype (all fixed BEFORE fixture freeze)

| # | Area | Change |
|---|------|--------|
| a | `sent_at` | Strict shared grammar (§1.6) replaces `datetime.fromisoformat`. Fixes the naive/aware `TypeError` crash, Python-version drift, JS local-time drift. |
| b | canonical tools | Sorted keys + integral-float→int minified JSON (§1.4). Prototype used insertion order and emitted `1.0`. |
| c | rounding/JSON | Half-even everywhere + JSON integer rule (§1.2, §1.3). |
| d | offsets | Code points everywhere (§1.1). |
| e | TS regexes | Unicode boundary emulation + flag translation (§1.7). |
| f | `clock-time` | Optional space consumed only when am/pm follows: `(?:\s?[AaPp][Mm])?`. `"at 12:30 tomorrow"` matches exactly `12:30` (prototype swallowed the trailing space). |
| g | `next_stable` | "If the first blocker were fixed" excludes only the first blocking finding **by identity/index**, not `start >` comparison (co-located second blocker no longer skipped). |
| h | Vertex rules | Dead `claude` rule removed (shadowed by the early-return branch). `2-?5` reordered before `2-?0\|gemini-2($\|-)` so `gemini-2.5-*` gets its own "Vertex Gemini 2.5 family" tier note. |
| i | Verdict | `CACHEABLE_WITH_ACTION` triggers whenever `upstream == "anthropic"` (any enablement mode). Claude-on-Vertex clean prompts now get it, phrased `"<display> (Anthropic upstream)"`. |
| j | Anthropic routing | `routing: null` — no more `[routing] none — no prompt_cache_key equivalent` recommendation. The fact moves into `gotchas`: "No prompt_cache_key equivalent — routing affinity cannot be steered; identical prefixes are the only lever." |
| k | Tokenizer label | `estimate(~N chars/tok)` reports the rate **actually used** — `~2.6` when the >30%-non-ASCII adjustment fires. |
| l | `_section_of` | Offsets landing inside a `\n\n` separator, at a section's `end`, or past the last section map to the nearest **preceding** section (prototype blanket-returned the last section). |
| m | Sequence grouping | Group key is `(provider_key, normalized_model)` (`_norm_model`: lowercase, `[ ._/]+` → `-`). Group header displays the first-seen raw model string. |
| n | Error messages | §1.8 (no `!r`; parser-neutral CLI JSON errors). |
| o | tiktoken | Hard requirement on exact paths (§3). No silent `except: pass` fallback — a missing/broken tokenizer raises with install instructions. |
| p | Docs | The detector count is **18** (a prototype doc said 17). |

Everything not listed here ports bug-for-bug from the prototype and is
pinned by fixtures — including: the epoch detector accepting only 10- or
13-digit values with a 16–19 prefix; the empty-model OpenAI fallback
producing a warning; `exact: true` (and therefore no estimate warning) for
openrouter-with-openai-upstream; sub-additive per-section token counts
(never assert `sum(sections) == total`); negative TTL gaps passing the
threshold check.

---

## 3. Tokenization

Exact-required policy:

- **OpenAI and xAI paths** (`effective = upstream or provider_key` in
  `{openai, xai}`, plus bedrock-with-openai-upstream) use **o200k_base**:
  `tiktoken` in Python, `js-tiktoken` in TypeScript. Special tokens are
  treated as ordinary text: Python `encode(text, disallowed_special=())`,
  TS `encode(text, [], [])` — `<|endoftext|>` in a prompt must encode, not
  throw.
  - Python: `tiktoken` is an optional extra — `pip install tryaii[cachelint]`.
    Importing `tryaii.cachelint` and analyzing heuristic-only providers must
    NOT require it; the first tokenize on an exact path without it raises:
    `ImportError: tiktoken is required for cachelint. Install with: pip install tryaii[cachelint]`
  - Node: `js-tiktoken` is a hard dependency of the package (static import
    confined to `src/cachelint/tokenizers.ts`; main `src/index.ts` does not
    re-export cachelint, so router users never load the rank data).
- **All other providers** use the heuristic (no offline exact tokenizer
  exists; tiktoken is WRONG for Claude): `tokens = max(1,
  round_half_even(cp_length / rate))` with rates
  `anthropic 3.5, gemini 4.0, vertex 4.0, openai 4.0, xai 3.8,
  openrouter 4.0, bedrock 3.7`; if the fraction of code points > 127 exceeds
  0.30 (strict), the rate drops to `min(rate, 2.6)` and the method label
  reports the adjusted rate (delta k).
- Empty text → `{tokens: 0, method: "empty", exact: true, note: ""}`.
- Every count is labeled: `method` (`tiktoken(o200k_base)` /
  `estimate(~N chars/tok)` / `empty`), `exact` (bool), `note` (provider
  guidance). xAI via tiktoken is `exact: false` (approximation).

### 3.1 Tokenizer parity spike (P1 gate)

Before any tokenize/analyze fixture is frozen,
`scripts/spike-cachelint-tokenizer-parity.mjs` must confirm `tiktoken` and
`js-tiktoken` produce **identical o200k_base counts** over the fixture
corpus plus special-token, Hebrew, emoji, and long mixed-text probes.
Results are recorded below.

> SPIKE RESULTS (2026-08-06, tiktoken 0.9.0 / js-tiktoken 1.x, Node 20.14,
> Python 3.12): **IDENTICAL** counts on all 14 probes —
> `[10, 101, 9, 13, 5, 4, 542, 1321, 193, 16, 8, 20, 0, 1]` — including
> `<|endoftext|>` / `<|im_start|>` as ordinary text, Hebrew, astral emoji,
> `\r\n`, and a 7k-char mixed corpus text. Gate PASSED; tokenize/analyze
> fixtures may freeze tiktoken-based counts.

---

## 4. CLI contract

```
tryaii cachelint <input.json | -> [--provider <p> --model <m>] [--json]
```

- **JSON mode** (default): the file (or stdin via `-`) contains the JSON
  envelope — a single `{prompt, llm: {provider, name}, sent_at?}` object, a
  list of them, or `{"inputs": [...]}`. `prompt` is a bare string or
  `{system, messages, tools}`.
- **Raw-text mode** (`--provider` present): the entire input is ONE raw
  prompt string (never parsed as JSON). `--model` is optional — an empty
  model resolves to the provider's default threshold with its standard
  warning. `--model` without `--provider` is a usage error.
- Output: the 78-column text report (verdict-first), or the full result JSON
  with `--json` (`indent=2`-style, byte-identical across SDKs).
- Exit codes: `0` analysis completed (findings do NOT change the exit code —
  cachelint warns, it never blocks); `1` runtime failure (file not found,
  missing tiktoken, unexpected error); `2` usage error (missing operand,
  unknown flag, invalid JSON, invalid input shape, unknown provider,
  `--model` without `--provider`).
- The Python handler reconfigures stdout to UTF-8 with `errors="replace"`
  (the report contains em-dashes; Windows consoles may not default to
  UTF-8).
- Golden comparisons (and the cross-CLI byte-diff test) normalize `\r\n` →
  `\n` before comparing.

---

## 5. Fixture governance

- Fixtures live in `shared/cachelint/fixtures/` and are **test-only**: never
  shipped in the wheel or npm package, never listed in
  `scripts/sync-shared.py`.
- Inputs and case names are hand-written; `expected` blocks and `*.golden.*`
  files are machine-generated by `scripts/gen-cachelint-fixtures.py` and
  hand-reviewed before commit. Run `--check` before any fixture commit.
- **Knowledge-base refresh policy** (thresholds/prices are time-sensitive by
  design): a KB refresh edits `shared/cachelint/providers.json`, runs
  `scripts/sync-shared.py` and the fixture generator, and lands as ONE
  `data(cachelint): refresh provider KB` commit touching the shared master,
  both package copies, and the regenerated `resolve/analyze/sequence/report/cli`
  fixtures together. `detectors/`, `canonical/`, and `tokenize/` fixtures
  must NOT churn on a data-only change — if they do, the change is not
  data-only and needs a spec review here first.
- Pre-merge (documented, runs locally; auto-skips in CI): build the Node CLI
  (`npm run build` in `packages/node`) and run
  `pytest packages/python/tests/test_cachelint_cli_parity.py` — the
  cross-CLI byte-diff over every `cli/` fixture.
