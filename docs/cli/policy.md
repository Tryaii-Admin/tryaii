# `tryaii policy` — control who builds your models and who sees your prompts

Many companies have rules about which AI vendors they may use: no Chinese models, only
American or European providers, only vendors they have a contract with, or only what their
cloud platform (AWS Bedrock, Azure, Google Vertex) offers. A **policy** tells tryaii those
rules once. Every routing decision then stays inside them: `route`, `eval`, `models`, and the
SDK (`Router`, `DREClient`, `chat`).

Policies need the **full catalog**, which carries the origin data. Run
[`tryaii login`](login.md) (free) first. With no policy set, tryaii routes over every model as
before.

```bash
tryaii policy set --origin no-prc-models          # save a policy
tryaii policy                                      # show it
tryaii route "Summarize this contract" --platform bedrock   # override for one call
tryaii policy clear                                # back to "all models"
```

## Two questions, two kinds of rule

A model raises two separate questions, and companies care about one, the other, or both:

1. **Who built the model?** The lab that trained it and, for a fine-tune, the lab behind its
   base model. A fine-tune of a Chinese model counts as Chinese: censorship, safety behaviour
   and legal exposure come with the weights. This applies even when the model runs on an
   American server, or on your own machine.
2. **Who receives your prompts?** The company running the server (the *host*). tryaii sends
   requests through OpenRouter, which can serve one model from many hosts. When a policy
   limits hosts, tryaii tells OpenRouter exactly which hosts it may use for every request
   (`provider.only`), so a prompt never reaches a host the policy excludes.

Countries are judged by **who controls the company**, not where a subsidiary is registered.
A Singapore or Hong Kong subsidiary of a Chinese company counts as Chinese. Taiwan is its own
country, never counted as China.

## Options

| Option | What it allows |
|---|---|
| `--origin no-prc-models` | Only models whose maker, and the maker of their base model, is not controlled from the People's Republic of China (mainland, Hong Kong, Macau). Hosts are not restricted. |
| `--origin no-prc-hosting` | Any model, but only served by hosts not controlled from the PRC. Use this when the concern is where data goes. A Chinese open-weight model served by an American host is allowed. |
| `--origin us-eu` | Models **and** hosts from the United States, the European Economic Area (the EU plus Iceland, Liechtenstein and Norway), the United Kingdom and Switzerland only. |
| `--providers a,b` | Only these companies, both as model maker and as host. `--providers anthropic,openai` allows Claude served by Anthropic and GPT served by OpenAI, but not Claude through Bedrock. Use company names (`anthropic`, `openai`, `google`, `mistral`, `meta`, `amazon`, `microsoft`, …) or an OpenRouter model-id prefix (`mistralai`, `x-ai`, `meta-llama`). |
| `--exclude-providers a,b` | Never these companies, as model maker, base-model maker or host. |
| `--allow-countries US,FR` | Only companies controlled from these countries (two-letter ISO codes), for makers and hosts. |
| `--deny-countries CN` | Never companies controlled from these countries, for makers and hosts. |
| `--platform bedrock` | Only models AWS Bedrock offers (`azure` = Azure AI Foundry, `vertex` = Google Vertex AI), served through that platform's OpenRouter endpoint. |

Options combine: a model must pass every rule you set.
`--origin no-prc-models --platform bedrock` means "non-Chinese models, served on Bedrock".

**Unknown means excluded.** A model whose origin tryaii cannot vouch for (a new model not yet
reviewed, a router that picks other models, a low-confidence entry) and a host whose owner is
not mapped yet are never allowed while a policy is active. A policy never silently falls back
to "all models". If the full catalog is not available (not logged in, or download failed), a
command with an active policy stops with:

```
Model filters (origin, providers, platform) need the full catalog. Run: tryaii login
```

## Saved policy vs. one-off flags

- `tryaii policy set …` saves the policy to `~/.tryaii/policy.json` (`TRYAII_DRE_DATA_DIR`
  overrides the folder). Each `set` only replaces the options you pass, so you can build a
  policy in steps.
- The saved policy applies to `route`, `eval` and `models`, and to the SDK by default.
- `--origin`, `--providers` and `--platform` on `route`, `eval` or `models` override the saved
  value of that option for one call.
- `tryaii policy clear` deletes the saved policy.

## Output

`route` shows the policy under the classification:

```
Policy: origin no-prc-models (176 of 322 models allowed)
```

`models` lists only the allowed models and, with the full catalog, ends every line with where
the model comes from. `US+CN` is a US company's fine-tune of a Chinese base model:

```
    - fireworks/ember-1 [fast] | $0.0009/0.0090 | origin: US+CN
```

`eval` prints `[eval] policy     : …` and records the policy in `summary.json`.

## SDK

```python
from tryaii import Router, DREClient, ModelPolicy

router = Router()                                        # the saved policy, if any
router = Router(policy=ModelPolicy(origin="us-eu"))      # this policy
router = Router(policy=None)                             # no policy
client = DREClient(policy={"providers": ["anthropic", "openai"]})
```

```ts
import { Router, DREClient } from 'tryaii';

const router = new Router({ policy: { origin: 'no-prc-models' } });
```

`router.policy_result` lists the allowed and excluded models (with the reason: `unknown`,
`origin`, `provider`, `platform` or `host`).

## Exit codes

`0` success, `1` broken policy file or full catalog unavailable, `2` bad option value.

The exact rules are specified in `shared/datasave/SPEC.md`.
