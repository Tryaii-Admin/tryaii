/**
 * Data save: model origin and provider policies (shared/datasave/SPEC.md).
 *
 * A `ModelPolicy` says which companies may build the models a user routes to
 * and which may receive their prompts. `evaluate()` applies it to a catalog's
 * `origins.json` (catalog contract v1.2, full catalog only) and returns the
 * allowed model ids plus, per model, the OpenRouter hosts a request may use.
 * The Router filters candidates with it; the OpenRouter integration pins
 * `provider.only` with it.
 *
 * Fail closed: an unknown model, an unmapped host or a missing `origins.json`
 * never passes an active policy.
 *
 * This module is pure data (no engine imports). Mirrors
 * `packages/python/tryaii/policy.py` (the reference); the fixtures in
 * shared/datasave/fixtures/ pin both. Error messages are byte-identical to
 * Python's (values are quoted the way Python's `repr` quotes them).
 */

import { readFileSync, unlinkSync } from 'node:fs';
import { join } from 'node:path';

import { dataDir, writeFileAtomic } from './auth/store.js';

export const PRC: ReadonlySet<string> = new Set(['CN', 'HK', 'MO']);
export const EEA: ReadonlySet<string> = new Set([
  'AT', 'BE', 'BG', 'HR', 'CY', 'CZ', 'DK', 'EE', 'FI', 'FR', 'DE', 'GR', 'HU', 'IE',
  'IT', 'LV', 'LT', 'LU', 'MT', 'NL', 'PL', 'PT', 'RO', 'SK', 'SI', 'ES', 'SE',
  'IS', 'LI', 'NO',
]);
export const US_EU: ReadonlySet<string> = new Set(['US', 'GB', 'CH', ...EEA]);

export const ORIGIN_PRESETS = ['no-prc-models', 'no-prc-hosting', 'us-eu'] as const;
export type OriginPreset = (typeof ORIGIN_PRESETS)[number];

export const PLATFORMS = ['bedrock', 'azure', 'vertex'] as const;
export type Platform = (typeof PLATFORMS)[number];

export const PLATFORM_HOSTS: Readonly<Record<Platform, readonly string[]>> = {
  bedrock: ['amazon-bedrock'],
  azure: ['azure'],
  vertex: ['google-vertex'],
};

/** Exclusion reasons, in the order they are tested (SPEC section 3). */
export const REASONS = ['unknown', 'origin', 'provider', 'platform', 'host'] as const;
export type ExclusionReason = (typeof REASONS)[number];

export const POLICY_FILE = 'policy.json';

export const MSG_NEEDS_FULL_CATALOG =
  'Model filters (origin, providers, platform) need the full catalog. Run: tryaii login';

const COUNTRY_RE = /^[A-Za-z]{2}$/;

/** The policy fields, in table order (SPEC section 2) -- the policy.json keys. */
export const POLICY_FIELDS = [
  'origin',
  'providers',
  'exclude_providers',
  'allow_countries',
  'deny_countries',
  'platform',
] as const;
export type PolicyField = (typeof POLICY_FIELDS)[number];

/** A policy value is invalid (unknown preset, platform, provider, country). */
export class PolicyError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'PolicyError';
  }
}

/** A policy is active but the catalog has no origin data (starter catalog). */
export class PolicyCatalogError extends PolicyError {
  constructor(message: string = MSG_NEEDS_FULL_CATALOG) {
    super(message);
    this.name = 'PolicyCatalogError';
  }
}

// ---------------------------------------------------------------- Python repr
/** Python `repr()` of a JSON-like value, for byte-identical error messages. */
export function pyRepr(value: unknown): string {
  if (value === null || value === undefined) return 'None';
  if (value === true) return 'True';
  if (value === false) return 'False';
  if (typeof value === 'number') {
    if (Number.isNaN(value)) return 'nan';
    if (!Number.isFinite(value)) return value > 0 ? 'inf' : '-inf';
    return String(value);
  }
  if (typeof value === 'string') return pyStrRepr(value);
  if (Array.isArray(value)) return `[${value.map(pyRepr).join(', ')}]`;
  if (typeof value === 'object') {
    const items = Object.entries(value as Record<string, unknown>).map(
      ([k, v]) => `${pyStrRepr(k)}: ${pyRepr(v)}`,
    );
    return `{${items.join(', ')}}`;
  }
  return String(value);
}

/** The characters Python's `str.isprintable()` rejects (space aside). */
const NON_PRINTABLE = /^[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}\p{Zs}]$/u;

function pyStrRepr(s: string): string {
  const quote = s.includes("'") && !s.includes('"') ? '"' : "'";
  let out = quote;
  for (const ch of s) {
    const cp = ch.codePointAt(0) as number;
    if (ch === quote || ch === '\\') out += '\\' + ch;
    else if (ch === '\t') out += '\\t';
    else if (ch === '\n') out += '\\n';
    else if (ch === '\r') out += '\\r';
    else if (ch !== ' ' && NON_PRINTABLE.test(ch)) {
      // Python's str.isprintable() is false: \xNN, \uNNNN or \UNNNNNNNN.
      if (cp < 0x100) out += '\\x' + cp.toString(16).padStart(2, '0');
      else if (cp < 0x10000) out += '\\u' + cp.toString(16).padStart(4, '0');
      else out += '\\U' + cp.toString(16).padStart(8, '0');
    } else out += ch;
  }
  return out + quote;
}

/** Python's `repr(type(x))` for the values a JS caller can pass. */
function pyTypeRepr(value: unknown): string {
  let name: string;
  if (value === null || value === undefined) name = 'NoneType';
  else if (typeof value === 'boolean') name = 'bool';
  else if (typeof value === 'number') name = Number.isInteger(value) ? 'int' : 'float';
  else if (typeof value === 'string') name = 'str';
  else if (Array.isArray(value)) name = 'list';
  else if (typeof value === 'function') name = 'function';
  else name = (value as object).constructor?.name ?? 'object';
  return `<class '${name}'>`;
}

/** Python truthiness of a JSON value (empty string / array / object are false). */
function truthy(value: unknown): boolean {
  if (value === null || value === undefined || value === false || value === 0 || value === '') {
    return false;
  }
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === 'object') return Object.keys(value as object).length > 0;
  return true;
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

/** Sorted like Python's `sorted()` on str (code point order). */
function pySorted(items: Iterable<string>): string[] {
  return [...items].sort((a, b) => {
    const x = [...a];
    const y = [...b];
    const n = Math.min(x.length, y.length);
    for (let i = 0; i < n; i++) {
      const d = (x[i].codePointAt(0) as number) - (y[i].codePointAt(0) as number);
      if (d !== 0) return d;
    }
    return x.length - y.length;
  });
}

/**
 * Python `str.isspace()` characters (JS `\s` differs: it has U+FEFF and lacks
 * U+001C-U+001F and U+0085).
 */
const PY_SPACE =
  '\\t\\n\\v\\f\\r\\u001c-\\u001f \\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000';
const PY_STRIP_RE = new RegExp(`^[${PY_SPACE}]+|[${PY_SPACE}]+$`, 'g');

/** Python `str.strip()`. */
function pyStrip(s: string): string {
  return s.replace(PY_STRIP_RE, '');
}

// ---------------------------------------------------------------- normalization
/** A list or comma-separated string of names -> lowercased, deduplicated, sorted. */
function names(value: unknown, label: string): string[] {
  if (value === null || value === undefined) return [];
  let items: unknown[];
  if (typeof value === 'string') items = value.split(',');
  else if (Array.isArray(value)) items = [...value];
  else throw new PolicyError(`${label} must be a list or a comma-separated string`);
  const out = new Set<string>();
  for (const item of items) {
    if (typeof item !== 'string') throw new PolicyError(`${label} must contain strings`);
    const name = pyStrip(item).toLowerCase();
    if (name) out.add(name);
  }
  return pySorted(out);
}

function countries(value: unknown, label: string): string[] {
  const out = new Set<string>();
  for (const name of names(value, label)) {
    const code = name.toUpperCase();
    if (!COUNTRY_RE.test(code)) {
      throw new PolicyError(`${label}: ${pyRepr(code)} is not a two-letter country code`);
    }
    out.add(code);
  }
  return pySorted(out);
}

function preset<T extends string>(
  value: unknown,
  allowed: readonly T[],
  label: string,
): T | null {
  let v: unknown = typeof value === 'string' ? pyStrip(value).toLowerCase() : value;
  if (v === '' || v === undefined) v = null;
  if (v !== null && !(allowed as readonly unknown[]).includes(v)) {
    throw new PolicyError(`unknown ${label} ${pyRepr(value)}; use one of: ${allowed.join(', ')}`);
  }
  return v as T | null;
}

/** `policy.json` / Python `to_dict()` shape (snake_case keys, SPEC section 2). */
export interface PolicyDict {
  origin?: string | null;
  providers?: string[] | string | null;
  exclude_providers?: string[] | string | null;
  allow_countries?: string[] | string | null;
  deny_countries?: string[] | string | null;
  platform?: string | null;
}

/** `new ModelPolicy({...})` options (camelCase; lists or comma-separated strings). */
export interface ModelPolicyInit {
  origin?: string | null;
  providers?: string[] | string | null;
  excludeProviders?: string[] | string | null;
  allowCountries?: string[] | string | null;
  denyCountries?: string[] | string | null;
  platform?: string | null;
}

/**
 * Which model makers and hosts are allowed (SPEC section 2).
 *
 * Every field is optional; a policy with no field set is inactive. Lists are
 * normalized (deduplicated, sorted; providers lowercased, countries
 * uppercased) so equal policies compare and serialize equally. Immutable.
 */
export class ModelPolicy {
  readonly origin: OriginPreset | null;
  readonly providers: readonly string[];
  readonly excludeProviders: readonly string[];
  readonly allowCountries: readonly string[];
  readonly denyCountries: readonly string[];
  readonly platform: Platform | null;

  constructor(init: ModelPolicyInit = {}) {
    // Same validation order as the Python dataclass's __post_init__.
    this.origin = preset(init.origin, ORIGIN_PRESETS, 'origin');
    this.platform = preset(init.platform, PLATFORMS, 'platform');
    this.providers = Object.freeze(names(init.providers, 'providers'));
    this.excludeProviders = Object.freeze(names(init.excludeProviders, 'exclude_providers'));
    this.allowCountries = Object.freeze(countries(init.allowCountries, 'allow_countries'));
    this.denyCountries = Object.freeze(countries(init.denyCountries, 'deny_countries'));
    Object.freeze(this);
  }

  // ------------------------------------------------------------ shape
  get active(): boolean {
    return Boolean(
      this.origin ||
        this.providers.length ||
        this.excludeProviders.length ||
        this.allowCountries.length ||
        this.denyCountries.length ||
        this.platform,
    );
  }

  get constrainsModels(): boolean {
    return Boolean(
      this.origin === 'no-prc-models' ||
        this.origin === 'us-eu' ||
        this.providers.length ||
        this.excludeProviders.length ||
        this.allowCountries.length ||
        this.denyCountries.length,
    );
  }

  get constrainsHosts(): boolean {
    return Boolean(
      this.origin === 'no-prc-hosting' ||
        this.origin === 'us-eu' ||
        this.providers.length ||
        this.excludeProviders.length ||
        this.allowCountries.length ||
        this.denyCountries.length ||
        this.platform,
    );
  }

  /** The policy.json object (same snake_case keys and order as Python's to_dict). */
  toDict(): {
    origin: string | null;
    providers: string[];
    exclude_providers: string[];
    allow_countries: string[];
    deny_countries: string[];
    platform: string | null;
  } {
    return {
      origin: this.origin,
      providers: [...this.providers],
      exclude_providers: [...this.excludeProviders],
      allow_countries: [...this.allowCountries],
      deny_countries: [...this.denyCountries],
      platform: this.platform,
    };
  }

  /** Parse a policy.json object; unknown keys are an error. */
  static fromDict(data: unknown): ModelPolicy {
    if (!isPlainObject(data)) throw new PolicyError('a policy must be a JSON object');
    const unknown = pySorted(
      Object.keys(data).filter((k) => !(POLICY_FIELDS as readonly string[]).includes(k)),
    );
    if (unknown.length > 0) {
      throw new PolicyError(`unknown policy field(s): ${unknown.join(', ')}`);
    }
    const get = (key: PolicyField): unknown => (data[key] === null ? undefined : data[key]);
    return new ModelPolicy({
      origin: get('origin') as string | undefined,
      providers: get('providers') as string[] | undefined,
      excludeProviders: get('exclude_providers') as string[] | undefined,
      allowCountries: get('allow_countries') as string[] | undefined,
      denyCountries: get('deny_countries') as string[] | undefined,
      platform: get('platform') as string | undefined,
    });
  }

  /**
   * This policy with each non-empty field of `override` (snake_case keys)
   * replacing ours -- CLI flags over the saved policy, field by field.
   */
  merged(override: Partial<Record<PolicyField, unknown>>): ModelPolicy {
    const current: Record<string, unknown> = this.toDict();
    for (const key of POLICY_FIELDS) {
      const value = override[key];
      if (value === undefined || value === null || value === '') continue;
      if (Array.isArray(value) && value.length === 0) continue;
      current[key] = value;
    }
    return ModelPolicy.fromDict(current);
  }

  /** The `Policy:` line text (SPEC section 5); "" when inactive. */
  summary(): string {
    const parts: string[] = [];
    if (this.origin) parts.push(`origin ${this.origin}`);
    if (this.providers.length) parts.push(`providers ${this.providers.join(',')}`);
    if (this.excludeProviders.length) {
      parts.push(`exclude-providers ${this.excludeProviders.join(',')}`);
    }
    if (this.allowCountries.length) parts.push(`allow-countries ${this.allowCountries.join(',')}`);
    if (this.denyCountries.length) parts.push(`deny-countries ${this.denyCountries.join(',')}`);
    if (this.platform) parts.push(`platform ${this.platform}`);
    return parts.join('; ');
  }

  /** True when both policies have the same (normalized) fields. */
  equals(other: ModelPolicy): boolean {
    return JSON.stringify(this.toDict()) === JSON.stringify(other.toDict());
  }
}

// ---------------------------------------------------------------- evaluation
/** `a/b:free` -> `a/b` (variants share their base model's origin entry). */
export function baseModelId(modelId: string): string {
  const i = modelId.indexOf(':');
  return i === -1 ? modelId : modelId.slice(0, i);
}

/** The OpenRouter `provider` request object. */
export interface ProviderPreferences {
  only: string[];
}

/** `evaluate()`'s output. */
export class PolicyResult {
  readonly policy: ModelPolicy;
  /** Allowed model ids, in input order. */
  allowed: string[] = [];
  /** `{modelId: reason}` for every excluded id, in input order. */
  excluded: Record<string, ExclusionReason> = {};
  /**
   * `{modelId: [host slugs]}` for allowed ids when the host constraint is
   * active -- the OpenRouter `provider.only` list.
   */
  hosts: Record<string, string[]> = {};

  constructor(policy: ModelPolicy) {
    this.policy = policy;
  }

  get total(): number {
    return this.allowed.length + Object.keys(this.excluded).length;
  }

  /**
   * The OpenRouter `provider` object for `modelId`, or null when no host
   * constraint applies.
   */
  providerPreferences(modelId: string): ProviderPreferences | null {
    if (!this.policy.constrainsHosts) return null;
    const hosts = Object.prototype.hasOwnProperty.call(this.hosts, modelId)
      ? this.hosts[modelId]
      : [];
    return { only: [...hosts] };
  }
}

type AnyMap = Record<string, unknown>;

/** Map provider names (org slugs or owned model namespaces) to org slugs. */
function resolveProviders(list: readonly string[], orgs: AnyMap, label: string): Set<string> {
  const aliases = new Map<string, string>();
  for (const slug of Object.keys(orgs)) aliases.set(slug.toLowerCase(), slug);
  for (const [slug, org] of Object.entries(orgs)) {
    const namespaces = isPlainObject(org) ? org.namespaces : undefined;
    for (const ns of Array.isArray(namespaces) ? namespaces : []) {
      const key = String(ns).toLowerCase();
      if (!aliases.has(key)) aliases.set(key, slug);
    }
  }
  const out = new Set<string>();
  const unknown: string[] = [];
  for (const name of list) {
    const slug = aliases.get(name);
    if (slug !== undefined) out.add(slug);
    else unknown.push(name);
  }
  if (unknown.length > 0) {
    throw new PolicyError(
      `unknown ${label}: ${unknown.join(', ')} (use company names such as ` +
        'anthropic, openai, google, mistral, or a model-id namespace)',
    );
  }
  return out;
}

type CountryTest = ((c: string) => boolean) | null;

function countryOk(country: unknown, policy: ModelPolicy, presetTest: CountryTest): boolean {
  if (country === null || country === undefined) return false;
  const c = country as string;
  if (presetTest !== null && !presetTest(c)) return false;
  if (policy.allowCountries.length && !policy.allowCountries.includes(c)) return false;
  if (policy.denyCountries.length && policy.denyCountries.includes(c)) return false;
  return true;
}

function modelPreset(origin: string | null): CountryTest {
  if (origin === 'no-prc-models') return (c) => !PRC.has(c);
  if (origin === 'us-eu') return (c) => US_EU.has(c);
  return null;
}

function hostPreset(origin: string | null): CountryTest {
  if (origin === 'no-prc-hosting') return (c) => !PRC.has(c);
  if (origin === 'us-eu') return (c) => US_EU.has(c);
  return null;
}

function mapOf(value: unknown): AnyMap {
  return isPlainObject(value) && truthy(value) ? value : {};
}

function listOf(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function has(map: AnyMap, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(map, key);
}

/**
 * Apply `policy` to `modelIds` using a catalog's `origins.json`.
 *
 * An inactive policy allows everything (`origins` may be null then). An
 * active policy with `origins` null throws `PolicyCatalogError`.
 */
export function evaluate(
  policy: ModelPolicy,
  origins: unknown,
  modelIds: Iterable<string>,
): PolicyResult {
  const ids = [...modelIds];
  const result = new PolicyResult(policy);
  if (!policy.active) {
    result.allowed = ids;
    return result;
  }
  if (!truthy(origins)) throw new PolicyCatalogError();
  const data = origins as AnyMap;

  const orgs = mapOf(data.orgs);
  const hosts = mapOf(data.hosts);
  const entries = mapOf(data.models);

  const providers = resolveProviders(policy.providers, orgs, 'provider');
  const excludedOrgs = resolveProviders(policy.excludeProviders, orgs, 'provider');

  const control = (orgSlug: unknown): unknown => {
    const org = truthy(orgSlug) && has(orgs, orgSlug as string) ? orgs[orgSlug as string] : null;
    return truthy(org) ? ((org as AnyMap).control_country ?? null) : null;
  };

  const modelTest = modelPreset(policy.origin);
  const hostTest = hostPreset(policy.origin);
  const platformHosts = policy.platform ? new Set(PLATFORM_HOSTS[policy.platform]) : null;

  const allowedHosts = new Set<string>();
  if (policy.constrainsHosts) {
    for (const [slug, host] of Object.entries(hosts)) {
      const org = isPlainObject(host) ? host.org : undefined;
      if (!truthy(org) || !has(orgs, org as string)) continue;
      if (!countryOk(control(org), policy, hostTest)) continue;
      if (providers.size && !providers.has(org as string)) continue;
      if (excludedOrgs.has(org as string)) continue;
      if (platformHosts !== null && !platformHosts.has(slug)) continue;
      allowedHosts.add(slug);
    }
  }

  for (const modelId of ids) {
    const base = baseModelId(modelId);
    const entry = has(entries, base) ? entries[base] : undefined;
    const e: AnyMap = isPlainObject(entry) ? entry : {};
    let reason: ExclusionReason | null = null;
    if (policy.constrainsModels) {
      if (!truthy(entry) || !truthy(e.known)) {
        reason = 'unknown';
      } else {
        const lineage = listOf(e.lineage) as string[];
        if (lineage.length === 0 || !lineage.every((o) => countryOk(control(o), policy, modelTest))) {
          reason = 'origin';
        } else if (
          (providers.size && !providers.has(e.developer as string)) ||
          lineage.some((o) => excludedOrgs.has(o))
        ) {
          reason = 'provider';
        }
      }
    }
    if (reason === null && policy.platform) {
      const platforms = e.platforms;
      const listed = Array.isArray(platforms)
        ? platforms.includes(policy.platform)
        : isPlainObject(platforms) && has(platforms, policy.platform);
      if (!truthy(entry) || !listed) reason = 'platform';
    }
    let modelHosts: string[] = [];
    if (reason === null && policy.constrainsHosts) {
      modelHosts = pySorted(
        (listOf(e.hosts) as string[]).filter((h) => typeof h === 'string' && allowedHosts.has(h)),
      );
      if (modelHosts.length === 0) reason = 'host';
    }
    if (reason === null) {
      result.allowed.push(modelId);
      if (policy.constrainsHosts) result.hosts[modelId] = modelHosts;
    } else {
      result.excluded[modelId] = reason;
    }
  }
  return result;
}

/**
 * Lineage control countries joined by `+` (`?` when unknown) -- the `models`
 * command's `origin:` column.
 */
export function originLabel(origins: unknown, modelId: string): string {
  if (!truthy(origins)) return '?';
  const data = origins as AnyMap;
  const models = mapOf(data.models);
  const base = baseModelId(modelId);
  const entry = has(models, base) ? models[base] : undefined;
  if (!truthy(entry) || !isPlainObject(entry) || !truthy(entry.known)) return '?';
  const orgs = mapOf(data.orgs);
  const out: string[] = [];
  for (const slug of listOf(entry.lineage) as string[]) {
    const org = has(orgs, slug) && isPlainObject(orgs[slug]) ? (orgs[slug] as AnyMap) : {};
    const country = org.control_country;
    if (!truthy(country)) return '?';
    if (!out.includes(country as string)) out.push(country as string);
  }
  return out.length ? out.join('+') : '?';
}

// ---------------------------------------------------------------- saved policy
export function policyPath(env: NodeJS.ProcessEnv = process.env): string {
  return join(dataDir(env), POLICY_FILE);
}

/**
 * The saved policy (`<data dir>/policy.json`), or an inactive one.
 *
 * A malformed file throws `PolicyError` -- a broken policy file must not
 * silently turn filtering off.
 */
export function loadSavedPolicy(env: NodeJS.ProcessEnv = process.env): ModelPolicy {
  const path = policyPath(env);
  let raw: string;
  try {
    raw = readFileSync(path, 'utf-8');
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return new ModelPolicy();
    throw new PolicyError(`could not read ${path}: ${(error as Error).message}`);
  }
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch (error) {
    throw new PolicyError(`${path} is not valid JSON: ${(error as Error).message}`);
  }
  return ModelPolicy.fromDict(data);
}

/** `JSON.stringify(v, null, 2)` with Python's default `ensure_ascii=True`. */
function pyJsonIndent2(value: unknown): string {
  // Python escapes everything outside ' '..'~' (DEL included); JSON.stringify
  // already escapes the C0 controls the same way.
  return JSON.stringify(value, null, 2).replace(
    /[^\x00-\x7e]/g,
    (ch) => '\\u' + ch.charCodeAt(0).toString(16).padStart(4, '0'),
  );
}

/** Write `policy` to `<data dir>/policy.json` (atomic). Returns the path. */
export function savePolicy(policy: ModelPolicy, env: NodeJS.ProcessEnv = process.env): string {
  const path = policyPath(env);
  const text = pyJsonIndent2(policy.toDict()) + '\n';
  writeFileAtomic(path, Buffer.from(text, 'utf-8'), '.policy-');
  return path;
}

/** Delete the saved policy; true when a file was removed. */
export function clearSavedPolicy(env: NodeJS.ProcessEnv = process.env): boolean {
  try {
    unlinkSync(policyPath(env));
    return true;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return false;
    throw error;
  }
}

/** What `Router` / `DREClient` accept as `policy`. */
export type PolicyLike = ModelPolicy | PolicyDict | 'auto' | null;

/**
 * `'auto'` = the saved policy; null = no policy; a policy.json-shaped object
 * (snake_case keys) or a `ModelPolicy` = that policy.
 */
export function resolvePolicy(
  policy: PolicyLike | undefined = 'auto',
  env: NodeJS.ProcessEnv = process.env,
): ModelPolicy {
  if (policy === null) return new ModelPolicy();
  if (policy === undefined) return loadSavedPolicy(env);
  if (policy instanceof ModelPolicy) return policy;
  if (typeof policy === 'string') {
    if (policy !== 'auto') {
      throw new PolicyError(
        `policy must be 'auto', None, a dict or a ModelPolicy; got ${pyRepr(policy)}`,
      );
    }
    return loadSavedPolicy(env);
  }
  if (isPlainObject(policy)) return ModelPolicy.fromDict(policy);
  throw new PolicyError(
    `policy must be 'auto', None, a dict or a ModelPolicy; got ${pyTypeRepr(policy)}`,
  );
}
