# Data save — model origin and provider policies (SPEC v1)

Companies need to control who builds the models they use and who receives their prompts.
"Data save" lets a user set one **policy** that every tryaii surface applies: `route`,
`eval`, `models`, the library `Router`, and `chat` through OpenRouter. (`diagnose` and
`demo` follow in a later version: diagnose prices the user's current model against the
catalog, so filtering it needs its own rules.)

This file is the contract for both SDKs. Python (`packages/python/tryaii/policy.py`) is the
reference implementation, Node (`packages/node/src/policy.ts`) mirrors it, and the fixtures in
`shared/datasave/fixtures/` pin the behaviour byte for byte.

## 1. Data: `origins.json` (catalog contract v1.2)

`origins.json` is an **optional** sixth data file of a catalog bundle. When present it is
listed in `manifest.files` with its sha256, so it is covered by the manifest signature like
every other file. The full catalog carries it; the starter catalog does not, so policies need
a logged-in user (owner decision). Clients that predate v1.2 ignore the file.

The tryaii team builds it from its model-origin and inference-host data when it cuts a catalog
release, resolved at build time so the SDKs only look things up:

```json
{
  "schema": 1,
  "as_of": "2026-10-10T06:00:00Z",
  "orgs": {
    "alibaba": {"name": "Alibaba Group", "country": "CN", "control_country": "CN",
                "flags": ["aa26_251a_named", "dod_1260h"], "namespaces": ["qwen"]}
  },
  "hosts": {
    "deepinfra": {"name": "DeepInfra", "org": "deepinfra", "headquarters": "US", "datacenters": ["US"]}
  },
  "models": {
    "fireworks/ember-1": {
      "developer": "fireworks",
      "lineage": ["fireworks", "moonshot"],
      "known": true,
      "kind": "model",
      "open_weights": false,
      "license": null,
      "hosts": ["fireworks"],
      "platforms": {"bedrock": {"id": "...", "geographies": ["us", "eu"]}}
    }
  }
}
```

- `orgs[*].namespaces` = the OpenRouter model-id namespaces the org owns (provider aliases).
- `orgs[*].control_country` is the ISO 3166-1 alpha-2 country of the **ultimate controller**
  (a Singapore subsidiary of a PRC company is CN). Taiwan is TW, never CN.
- `models` is keyed by **base** model id: a variant (`a/b:free`, `a/b:thinking`) uses the entry
  of the id before the first `:`.
- `lineage` = sorted unique org slugs of the developer and the root base model's developer.
  A fine-tune of a Chinese base model therefore carries a CN org (owner decision).
- `known` is false when the developer cannot be resolved, the curated confidence is `low`, or
  the entry is a router (`kind: "router"`, e.g. `openrouter/pareto-code`). Unknown models are
  excluded by any policy that constrains models (owner decision: fail closed).
- `hosts` = sorted OpenRouter host slugs serving the model (endpoint tag prefix), from the last
  24 hours. `[]` means no host data, which excludes the model under any host constraint.
- `hosts[*].org` null means the host's company is not mapped yet: such a host is never allowed
  under a host constraint.

## 2. The policy

```json
{"origin": "no-prc-models", "providers": ["anthropic", "openai"], "exclude_providers": [],
 "allow_countries": [], "deny_countries": [], "platform": null}
```

| Field | Values | Meaning |
|---|---|---|
| `origin` | `null`, `no-prc-models`, `no-prc-hosting`, `us-eu` | preset (section 3) |
| `providers` | org slugs | only these companies: model developer AND host company |
| `exclude_providers` | org slugs | never these companies (developer, lineage or host) |
| `allow_countries` | ISO-2 codes | only these control countries (lineage and hosts) |
| `deny_countries` | ISO-2 codes | never these control countries (lineage and hosts) |
| `platform` | `null`, `bedrock`, `azure`, `vertex` | only models the platform serves, routed through its OpenRouter host |

A policy is **active** when any field is set. Empty lists and null mean "not set".
Provider names are matched case-insensitively against org slugs; an OpenRouter model
namespace that an org owns is accepted as an alias (`mistralai` → `mistral`, `x-ai` → `xai`).
An unknown provider name, preset, platform or a malformed country code is an error, never
silently ignored. Country codes are upper-cased; `allow_countries` and `deny_countries` must
be two ASCII letters.

## 3. Evaluation (exact)

Constants:
- `PRC` = {CN, HK, MO}
- `EEA` = EU27 (AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE) + IS LI NO
- `US_EU` = {US} ∪ EEA ∪ {GB, CH}
- `PLATFORM_HOSTS` = {bedrock: [amazon-bedrock], azure: [azure], vertex: [google-vertex]}

Country test for a **model** lineage country `c` (null fails every active test):
- `origin = no-prc-models` → `c ∉ PRC`; `origin = us-eu` → `c ∈ US_EU`
- `allow_countries` set → `c ∈ allow_countries`; `deny_countries` → `c ∉ deny_countries`

Country test for a **host** control country `c` (null fails every active test):
- `origin = no-prc-hosting` → `c ∉ PRC`; `origin = us-eu` → `c ∈ US_EU`
- `allow_countries` / `deny_countries` as above

The **model constraint** is active when `origin ∈ {no-prc-models, us-eu}` or any of
`providers`, `exclude_providers`, `allow_countries`, `deny_countries` is set. A model passes it
when its entry exists, `known` is true, every lineage org's `control_country` passes the model
country test, its `developer` is in `providers` (when set) and no lineage org is in
`exclude_providers`.

The **host constraint** is active when `origin ∈ {no-prc-hosting, us-eu}` or any of
`providers`, `exclude_providers`, `allow_countries`, `deny_countries`, `platform` is set. A host
`h` is allowed when `hosts[h].org` is mapped, that org's `control_country` passes the host
country test, the org is in `providers` (when set) and not in `exclude_providers`, and
`h ∈ PLATFORM_HOSTS[platform]` (when set). A model passes when at least one of its `hosts` is
allowed. Its OpenRouter host list is `sorted(model.hosts ∩ allowed hosts)`.

The **platform constraint** (when `platform` is set): `models[id].platforms` has that platform.

A model is **allowed** when it passes every active constraint. Exclusion reasons, first match
wins, in this order: `unknown` (no entry, or `known` false while the model constraint is
active), `origin` (a lineage country fails), `provider` (developer not in `providers`, or a
lineage org excluded), `platform`, `host` (no allowed host).

## 4. Where the policy applies

- **Router**: candidates are filtered before scoring. Benchmark coverage still comes from the
  whole catalog (unchanged). If no model is allowed, the result is empty (`best_model` = "").
- **chat / stream / route_and_chat**: when the host constraint is active the OpenRouter request
  carries `"provider": {"only": [<host list of the chosen model>]}`. Without a host constraint
  the request is unchanged.
- **Daemon**: the CLI sends the allowed model ids (`allowedModels`) with each request; the daemon
  never reads the saved policy itself.
- **Saved policy**: `<data dir>/policy.json` (`TRYAII_DRE_DATA_DIR` or `~/.tryaii`), written by
  `tryaii policy set`. Library `Router(policy="auto")` (the default) applies it; `policy=None`
  disables policies; an explicit policy replaces the saved one. CLI flags override the saved
  policy field by field.
- **No full catalog**: an active policy on a bundle without `origins.json` raises
  `PolicyCatalogError` (CLI: stderr message, exit 1):
  `Model filters (origin, providers, platform) need the full catalog. Run: tryaii login`
  Never route unfiltered when a policy is active.

## 5. CLI (byte-identical across SDKs)

- `route`, `eval`, `models` accept `--origin <preset>`, `--providers <a,b>`,
  `--platform <name>`. Exit 2 for a bad value; exit 1 for a broken saved policy file or a
  catalog without `origins.json`.
- `eval` prints `[eval] policy     : <summary> (<allowed> of <total> models allowed)` and
  `summary.json` gains `policy` (the policy fields plus `allowedModels`, `totalModels`).
- `route` adds, after the `Classifier:` line, when a policy is active:
  `Policy: <summary> (<allowed> of <total> models allowed)`.
- `models` filters by the policy and prints the `Policy:` line under its header; with
  `origins.json` loaded each line ends with ` | origin: <countries>` (lineage control
  countries in lineage order, deduplicated, joined by `+`; `?` when unknown), and `--json`
  entries gain `"origin"`.
- `tryaii policy` prints `Saved policy: <summary>` or
  `No policy saved. All models are allowed.`; `tryaii policy set [--origin] [--providers]
  [--exclude-providers] [--allow-countries] [--deny-countries] [--platform]` merges the given
  fields into the saved policy and prints `Saved policy: <summary>` (no flag: exit 2);
  `tryaii policy clear` prints `Policy cleared.` or `No policy saved.`.

`<summary>` is the active fields in table order, `; `-joined: `origin no-prc-models`,
`providers anthropic,openai`, `exclude-providers x`, `allow-countries US,FR`,
`deny-countries CN`, `platform bedrock`.
