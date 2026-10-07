# tryaii model catalog — contract v1.1

This is the client-side specification of the model catalog: the bundle format
both SDKs (Python and Node) route on, how a logged-in client downloads, verifies,
caches and refreshes the full catalog, and the exact CLI behaviour. It builds on
[the auth contract](../auth/CONTRACT-auth-v1.md) (v1.1); CLI output is
byte-identical across the two SDKs.

User-facing text calls the two catalogs **starter** (ships in the package) and
**full** (after `tryaii login`).

## 1. Bundle format (same for starter and full)

A bundle is a directory (on disk) or a `files` map (on the wire) with exactly:

| file | content |
|---|---|
| `manifest.json` | see below |
| `models.json` | `{"models": [...], ...metadata keys}`; each model is the `ModelInfo.to_dict()` shape (`model_id`, `provider`, `benchmark_scores`, `capabilities`, `pricing {input_per_1k, output_per_1k}`, `latency`, `tokens_per_second`, `ttft_ms`, `description`) |
| `benchmarks.json` | the benchmark taxonomy, weights and classifier labels. Exact shape: Appendix A |
| `normalization_ranges.json` | `{"benchmarks": {<name>: {lo, hi, fallback, description}}, ...metadata keys}` |
| `centroids.json` | `{"metadata": {model, dimension, benchmark_count, benchmark_fingerprint}, "centroids": {<name>: [floats]}}`, for `manifest.embedding_model` |
| `training_queries.json` | `{"benchmarks": {<name>: {description, queries: [strings]}}, ...metadata keys}` |

`manifest.json`:
```json
{"schema": 1, "kind": "starter", "version": "2026.10.07.1",
 "embedding_model": "all-MiniLM-L6-v2", "created_at": "2026-10-07T12:00:00Z",
 "counts": {"models": 45, "benchmarks": 16},
 "full_counts": {"models": 322, "benchmarks": "..."},
 "files": {"models.json": "<sha256 hex>", "benchmarks.json": "...",
           "normalization_ranges.json": "...", "centroids.json": "...",
           "training_queries.json": "..."},
 "signature": null, "key_id": null}
```
- `kind`: `starter` | `full`. `full_counts` is present in starter manifests only
  (used by the nudge text); omitted in full manifests.
- **User-facing model counts** (nudge, `login`, `whoami`) are ROUTABLE models: entries
  whose id is not a `:free` variant. The starter manifest's `full_counts.models` holds
  the full catalog's routable count; `whoami` and `login` count routable models in
  the loaded full bundle. `counts.models` stays "every entry".
- `version`: `YYYY.MM.DD.N`, unique per kind.
- **Canonical file text** = Python `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`,
  UTF-8, no trailing newline. Every bundle file except `manifest.json` is stored and
  transferred in canonical text; `files[name]` is the sha256 of those UTF-8 bytes.
  Clients hash the exact text they received/read — they never re-serialize to verify.
- `signature`/`key_id`: see section 6. The starter bundle shipped in the package
  may leave both null.
- `schema` greater than what a client supports -> the client refuses the bundle
  (exact behaviour and message in section 5).

## 2. Starter and full catalogs

- The **starter** bundle ships in both packages as package data (identical bytes
  in each) and is the package's built-in routing data. It holds about 45
  well-known models and only the benchmarks with good coverage among them (16 in
  this release).
- The **full** bundle is published by the tryaii team and downloaded by logged-in
  clients (sections 4 and 5). It is never shipped in a package.
- Both are built from the same data, so a model scores the same on both catalogs
  for every benchmark they share: the starter's normalization ranges are copied
  verbatim from the full build, and the starter's centroids, training queries and
  ranges contain ONLY the starter benchmarks (Appendix A rules).
- Maintainers regenerate the starter bundle; contributors never need to.

## 3. Releases

- The server has at most one **live** full release at a time. A new release gets a
  new `version`; a version's content never changes once published.
- Clients only ever see the live release (section 4) and keep only the newest
  version they downloaded (section 5).

## 4. `GET /v1/catalog/live`

- `Authorization: Bearer <access token>` (auth contract). 401 `invalid_token` as /me.
- Requires the entitlement `catalog:full` (auth contract, section 2), checked
  on every request. Missing -> 403 `{"error": "insufficient_entitlement"}`.
- No live release -> 404 `{"error": "no_live_release"}`.
- `If-None-Match: "<version>"` equal to the live version -> 304, empty body.
- 200 body:
```json
{"manifest": { ...manifest.json object... },
 "files": {"models.json": "<canonical text>", "benchmarks.json": "<canonical text>",
           "normalization_ranges.json": "...", "centroids.json": "...",
           "training_queries.json": "..."}}
```
  Header `ETag: "<version>"`, `Cache-Control: private, no-store`. Responses are
  gzip-compressed when the client accepts it. The server checks each hash before
  serving; a mismatch -> 500 `{"error": "release_corrupt"}`.
- Too many requests -> 429 `slow_down` with a `Retry-After` header. Clients check
  at most once a day (section 5), far below the limit.

## 5. SDK behaviour

### Cache (under the data dir, `TRYAII_DRE_DATA_DIR` or `~/.tryaii`)
- `catalog/full/<version>/` — the six bundle files exactly as received (manifest
  written as received JSON, files as the canonical text).
- `catalog/state.json` — `{"version": "<live version or null>", "checked_at": "<ISO Z>"}`.
- `catalog/nudge.json` — `{"last_shown": "<YYYY-MM-DD local date>"}`.
- Writes are atomic (temp + rename, same helper as credentials). Only the newest
  version directory is kept.
- `logout` deletes `catalog/full/` and `catalog/state.json` (also when the server
  revoke fails).

### Which catalog (library + CLI), `catalog="auto"` (default)
Library: `Router(..., catalog="auto" | "starter" | "full")`, same option name in Node.
1. No credentials file -> **starter** (+ nudge in the CLI).
2. Credentials present:
   - If `state.checked_at` is missing or older than 24 h: refresh the access token if
     needed (auth contract rules), then `GET /v1/catalog/live` with `If-None-Match` of the
     cached version. 200 -> verify every hash, write the new version, use it.
     304 -> touch `checked_at`, use cache.
   - Refresh `invalid_grant` or catalog/me 401 -> **session ended**: delete the
     credentials file and the full cache, and FAIL (CLI: stderr
     `Your session has ended. Run: tryaii login` exit 1; library: raise
     `SessionEndedError` with the same message). No silent fallback.
   - 403 `insufficient_entitlement` -> starter, CLI stderr line
     `This account does not have access to the full catalog; using the starter catalog.`
   - Network failure / 5xx / 429 / 404 `no_live_release` / hash mismatch / schema too
     new -> use the cached full catalog if one exists; otherwise starter with CLI
     stderr `Could not download the full catalog; using the starter catalog for now.`
     (schema too new instead prints `The full catalog needs a newer tryaii version; using the starter catalog.`)
3. `catalog="starter"`: always starter, no network, no nudge.
4. `catalog="full"`: like auto, but if not logged in -> fail with
   `Not logged in. Run: tryaii login` (library: `LoginRequiredError`).
The check never blocks longer than one request (10 s timeout, auth contract transport).

### Nudge
When `route`, `eval` or `models` runs on the starter catalog because the user
is not logged in (rule 1), print to **stderr** at most once per local calendar day:
```
Routing on the starter catalog (<counts.models> models). Log in for free to use the full catalog (<full_counts.models> models): tryaii login
```
Suppressed by `TRYAII_NO_BANNER`. Never printed by the library API, only the CLI.

### CLI changes (byte-identical across SDKs)
- `tryaii login`: after `Logged in as <email>.`, download the catalog immediately:
  success -> `Downloaded the full catalog (<n> models).`; any failure ->
  `Could not download the full catalog now; it will be fetched on next use.`
  (stdout, exit code stays 0).
- `tryaii whoami`: third line `Catalog: full, release <version> (<n> models)` when a
  full cache exists, else `Catalog: not downloaded yet`. `--json` unchanged (server JSON).
- `tryaii models`: unchanged output format, lists the catalog in use.
- Global HELP login line becomes:
  `  login                 Sign in with your tryaii.com account (free; unlocks the full model catalog)`
- HELP_LOGIN gains, after the credentials paragraph:
  `After signing in, the full model catalog is downloaded and kept up to date`
  `automatically (checked at most once a day).`

## Appendix A — benchmarks.json shape
Everything the scoring engine and classifier need about benchmarks (taxonomy,
importance weights, random-chance floors, the classifier's category labels) is in
this file, so the same engine code routes both catalogs. Normalization ranges stay
in `normalization_ranges.json`.

```json
{
  "families": [
    {"id": "math_reasoning", "label": "Math / reasoning"},
    {"id": "human_preference", "label": "Chatbot Arena (human preference)"}
  ],
  "benchmarks": [
    {"name": "GPQA",
     "description": "Graduate-level science question answering",
     "family": "math_reasoning",
     "broad_category": "EDUCATIONAL",
     "subcategories": ["ACADEMIC_INSTRUCTION", "RESEARCH_METHODOLOGY"],
     "weight": 1.3,
     "random_chance_floor": 10},
    {"name": "Chatbot Arena Elo",
     "description": "Human-rated conversational quality",
     "family": "human_preference",
     "broad_category": "CONVERSATIONAL",
     "subcategories": ["PERSONAL_ADVICE", "RECOMMENDATIONS"],
     "weight": 1.4,
     "random_chance_floor": null}
  ]
}
```
(Stored, like every bundle data file, as canonical text; the example is pretty-printed.)

| field | type | meaning / who reads it |
|---|---|---|
| `families` | array of `{id, label}` | Display grouping (the benchmark "hierarchy"). Only families used by the bundle's benchmarks are listed. Not read by the engine. |
| `benchmarks` | array | One entry per benchmark. **Array order = display order** (`tryaii benchmarks`, `BenchmarkRegistry.names`); canonical text keeps array order. |
| `name` | string, unique | The join key. The set of names MUST equal the key sets of `normalization_ranges.json` `benchmarks`, `centroids.json` `centroids` and `training_queries.json` `benchmarks`; every key in any model's `benchmark_scores` should be one of them. Loaders refuse a bundle whose sets differ. |
| `description` | string | Human-readable description (`tryaii benchmarks`, `BenchmarkDefinition.description`). |
| `family` | string | One of `families[].id`. Display only. |
| `broad_category` | string | Classifier label: the category reported when this benchmark is the prompt's top match (`ClassificationResult.broad_category`). Values in use: `TECHNICAL`, `EDUCATIONAL`, `BUSINESS`, `CONVERSATIONAL`, `CREATIVE`. |
| `subcategories` | non-empty array of strings | First element = the classifier's `subcategory` label for this benchmark (`GENERAL` if empty). |
| `weight` | number > 0 | Importance weight in the quality aggregate: per-term weight `= similarity × weight × coverage^COVERAGE_EXPONENT (× IMPUTED_TERM_WEIGHT if imputed)`. 1.0 is neutral. |
| `random_chance_floor` | number or null | Load-time plausibility floor: a model score strictly below it is dropped as corrupt (`ModelInfo.from_dict`). null = no floor. |

Rules:
- A benchmark's `normalization_ranges.json` entry is `{lo, hi, fallback, description}`
  (loaders ignore extra keys); the engine normalizes `clamp((raw - lo) / (hi - lo), 0, 1)`.
- In a starter bundle, `benchmarks.json`, ranges, centroids and training queries contain only
  the starter benchmarks, the ranges are copied verbatim from the full build, and the
  starter's `models.json` keeps only scores for starter benchmarks.
- Not catalog data (engine constants, stay in code in both SDKs): `EPS_UNIT`,
  `COVERAGE_EXPONENT`, `IMPUTED_TERM_WEIGHT`, `IMPUTATION_SHRINKAGE_K`, the cost/speed
  anchors, `DEFAULT_BENCHMARK_WEIGHT` (1.0) and the 0-100 fallback range for a benchmark
  not in the bundle (user-registered custom benchmarks), the classifier fallback label
  (`TECHNICAL` / `CODE_TECHNICAL`), and the difficulty exemplars.
- `counts.models` in a manifest is the length of `models.json` `models` (it includes
  ephemeral `:free` ids; the router skips those, so the routable count can be lower).
- Adding a field is backwards compatible (loaders ignore unknown keys); renaming or
  re-typing one needs a `schema` bump.

## 6. Signing (catalog contract v1.1, 2026-10-04)

Purpose: the SDK trusts a full catalog only if it was signed with a key the
tryaii team holds offline. Checksums alone (section 1) detect corruption, not
forgery, because the manifest travels with the files.

- **Algorithm:** Ed25519 (RFC 8032), pure Ed25519 (no prehash).
- **Signed message:** the manifest object with the keys `signature` and `key_id`
  REMOVED, serialized as canonical text (section 1 rule:
  `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`),
  UTF-8 encoded. Manifests MUST be ASCII-only (every key and string value); clients
  reject non-ASCII manifests, so Python and Node canonical forms are identical.
- **`signature`:** base64url without padding of the 64-byte signature.
- **`key_id`:** `[a-z0-9-]{3,64}`, names the public key, e.g. `tryaii-catalog-2026-1`
  (production) or `dev-…` (development).
- **Who signs:** the tryaii team, with a private key kept offline. The private
  key is never committed and never stored on the API server; the server serves
  the manifest's `signature` and `key_id` unchanged and never signs anything.
- **Trusted public keys:** `shared/catalog/trusted_keys.json`
  `{"keys": [{"key_id": "...", "public_key": "<base64url raw 32 bytes>", "env": "production"|"development"}]}`,
  shipped in both packages (identical bytes). The released package must contain
  only `production` keys; the release checks fail if a `development`
  key is shipped or if no `production` key exists. `TRYAII_CATALOG_TRUSTED_KEYS`
  (path to a JSON file of the same shape) REPLACES the built-in list — for
  development and tests only; documented as such.
- **Clients (both SDKs):** a full catalog from `GET /v1/catalog/live` is accepted only
  if `key_id` is in the trusted list and the signature verifies. Missing signature,
  unknown `key_id`, or a bad signature is handled exactly like a hash mismatch
  (section 5: cached full catalog if any, else starter + the download-failure line).
  Verification happens before anything is written to the cache; the cached
  manifest keeps the signature and is re-verified when the cache is loaded.
  The starter bundle shipped in the package is NOT required to be signed.
  Python verifies with a vendored, verify-only Ed25519 implementation (no new
  runtime dependency) tested against the RFC 8032 test vectors; Node uses
  `crypto.verify(null, msg, publicKey, sig)`.
