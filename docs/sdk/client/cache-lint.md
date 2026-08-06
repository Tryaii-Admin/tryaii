# `cache_lint="warn"` — SDK cache lint + runtime verification

Lint every outgoing chat request for prompt-cache readiness **at the moment
it leaves** — after routing, message assembly, and truncation — and verify
the prediction against the provider's actual `usage` response. This is the
SDK seat of the [cachelint engine](../../cli/cachelint.md): the CLI sees what
you paste into it; the hook sees the real rendered payload of every call.

```python
# Python — DREClient, OpenRouterIntegration, and AsyncDREClient all accept it
from tryaii import DREClient

client = DREClient(api_key="sk-or-...", cache_lint="warn")
client.chat("...")   # warnings, if any, print to stderr; the call is never blocked
```

```ts
// Node — DREClient and OpenRouterIntegration
import { DREClient } from 'tryaii';

const client = new DREClient({ apiKey: 'sk-or-...', cacheLint: 'warn' });
```

`TRYAII_CACHE_LINT=warn` enables it without code changes; an explicit
constructor value always wins (`cache_lint="off"` beats the env var).
Default is off.

## What you get

**Pre-flight warnings** — before the request is sent, the assembled messages
run through the cachelint engine (18 detectors, per-model token floors,
stable-prefix computation). A problem prints 1–3 stderr lines:

```
[tryaii cachelint] EFFECTIVELY_UNCACHEABLE for anthropic/claude-fable-5: Dynamic content too early: the stable prefix is only ~5 tok, below the 512 minimum — in practice nothing will cache until it moves.
[tryaii cachelint]   first blocker: uuid at messages[0]:system+16 (0% in)
```

**Runtime verification** — the hook remembers what it predicted per prompt
shape. From the **second** call with the same shape onward (the first call is
the expected cache write), a shape predicted cacheable that reports zero
cached tokens in `usage` warns once:

```
[tryaii cachelint] VERIFY_MISS for google/gemini-2.5-pro: predicted ~2244 tok cacheable prefix, but usage reports 0 cached tokens on repeat call #2 of this prompt shape — a silent invalidator or missing provider support may be the cause
```

## Noise policy

- **Problems only.** Warnings fire for `BELOW_THRESHOLD`,
  `EFFECTIVELY_UNCACHEABLE`, and `UNKNOWN_THRESHOLD` *with* blocking
  findings. Healthy prompts are silent; a bare unknown threshold (a
  knowledge-base gap, e.g. a DeepSeek slug) is silent too.
- **Once per prompt shape, per client instance.** The shape key is a hash of
  (model slug, canonical rendered prompt). Agent loops repeating one prompt
  see one warning, not thousands. Memory is capped (1024 shapes, FIFO) and a
  shape idle for over an hour is treated as fresh. A new client instance
  starts clean — there is no cross-run persistence.
- **Fail-open, absolutely.** A lint failure of any kind — engine error,
  missing tokenizer, broken stderr — never raises and never blocks the API
  call. There is deliberately no "strict" mode.

## Notes and caveats

- **Python + OpenAI/xAI-routed prompts** need the tokenizer extra for exact
  analysis: `pip install tryaii[cachelint]`. Without it the hook skips those
  prompts and prints a one-time install hint. The Node SDK bundles its
  tokenizer.
- **Streaming** verification is best-effort: OpenRouter only includes a
  `usage` chunk when usage accounting is enabled upstream; the hook reads it
  when present and stays silent otherwise. `chat()` is the first-class path.
- `CACHEABLE_WITH_ACTION` (Anthropic-routed prompts that would need
  `cache_control` breakpoints) is intentionally silent today: the SDK does
  not send cache breakpoints yet, so zero cached tokens is the *correct*
  outcome — warning would be noise. This becomes actionable once the SDK can
  set breakpoints.
- Python validates the option strictly (`ValueError` for anything other than
  `"off"`/`"warn"`); Node relies on the TypeScript union and treats unknown
  runtime values as off. Env values are lenient in both.

## See also

- [`tryaii cachelint` CLI](../../cli/cachelint.md) — the same engine over
  files/stdin, with the full report and `--json`.
- [DREClient](README.md) · [OpenRouterIntegration](openrouter.md) ·
  [AsyncDREClient](async.md)
