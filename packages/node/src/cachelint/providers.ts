/**
 * Provider caching knowledge base — loader + resolution.
 *
 * The knowledge base is DATA: `data/providers.json`, synced from the repo's
 * `shared/cachelint/providers.json` master (byte-compared across both SDKs by
 * test_parity). This module loads it and resolves (provider, model) into an
 * effective spec + minimum cacheable tokens, including gateway upstream
 * delegation (openrouter -> anthropic/openai/gemini/xai tiers, vertex+claude
 * -> anthropic tiers). Mirrors the Python reference module byte-for-byte in
 * every user-facing string.
 */

import { readFileSync } from 'node:fs';

export interface ThresholdRule {
  pattern: string;
  min_tokens: number | null;
  note: string;
}

export interface ProviderSpec {
  key: string;
  display: string;
  enablement: 'automatic' | 'explicit' | 'hybrid' | 'pass-through';
  enablement_detail: string;
  action: string | null;
  threshold_rules: readonly ThresholdRule[];
  default_min_tokens: number | null;
  default_min_note: string;
  increment_tokens: number | null;
  ttl_summary: string;
  ttl_seconds: number | null;
  read_discount: string;
  write_cost: string;
  verify_field: string;
  routing: string | null;
  batch_note: string;
  gotchas: readonly string[];
  sources: readonly string[];
}

export interface Resolved {
  spec: ProviderSpec;
  provider_key: string;
  model: string;
  min_tokens: number | null;
  tier_note: string;
  warnings: string[];
  upstream: string | null;
}

interface RawKb {
  version: string;
  meta: { knowledge_base: string; caveat: string };
  aliases: Record<string, string>;
  rule_sets: Record<string, { pattern: string; min_tokens: number | null; note: string }[]>;
  providers: Record<
    string,
    Omit<ProviderSpec, 'key' | 'threshold_rules'> & { rule_set: string | null }
  >;
}

function loadKb() {
  const raw = JSON.parse(
    readFileSync(new URL('./data/providers.json', import.meta.url), 'utf-8'),
  ) as RawKb;
  const ruleSets: Record<string, ThresholdRule[]> = {};
  for (const [name, rules] of Object.entries(raw.rule_sets)) {
    ruleSets[name] = rules.map((r) => ({ pattern: r.pattern, min_tokens: r.min_tokens, note: r.note }));
  }
  const providers: Record<string, ProviderSpec> = {};
  for (const [key, spec] of Object.entries(raw.providers)) {
    const { rule_set, ...rest } = spec;
    providers[key] = {
      key,
      ...rest,
      threshold_rules: rule_set ? ruleSets[rule_set] : [],
    };
  }
  return { aliases: raw.aliases, ruleSets, providers, meta: raw.meta };
}

const KB = loadKb();

export const PROVIDERS: Record<string, ProviderSpec> = KB.providers;
const RULE_SETS = KB.ruleSets;
const ALIASES = KB.aliases;

export const META: Record<string, string> = {
  tool: 'cachelint',
  knowledge_base: KB.meta.knowledge_base,
  caveat: KB.meta.caveat,
};

/** Normalize a model name for pattern matching: lowercase, unify separators. */
export function normModel(name: string): string {
  return (name || '').trim().toLowerCase().replace(/[ ._/]+/g, '-');
}

/** For OpenRouter model strings like 'anthropic/claude-opus-4.8'. */
function detectUpstream(normalized: string): string | null {
  if (/claude|anthropic/.test(normalized)) return 'anthropic';
  if (/gpt|openai|o[134](-|$)/.test(normalized)) return 'openai';
  if (/gemini|google/.test(normalized)) return 'gemini';
  if (/grok|x-ai|xai/.test(normalized)) return 'xai';
  return null;
}

function matchRules(rules: readonly ThresholdRule[], normalized: string): ThresholdRule | null {
  for (const rule of rules) {
    if (new RegExp(rule.pattern).test(normalized)) return rule;
  }
  return null;
}

/** Resolve provider + model into a spec + effective minimum tokens. */
export function resolve(provider: string, model: string): Resolved {
  const normProvider = (provider || '').trim().toLowerCase().replace(/[ ._/]+/g, '-');
  const key = ALIASES[normProvider];
  if (key === undefined) {
    const known = [...new Set(Object.values(ALIASES))].sort().join(', ');
    throw new Error(`Unknown provider '${provider}'. Known: ${known}`);
  }
  const spec = PROVIDERS[key];
  const normalized = normModel(model);
  const warnings: string[] = [];
  let upstream: string | null = null;

  if (key === 'openrouter') {
    upstream = detectUpstream(normalized);
    let minTokens: number | null;
    let tierNote: string;
    if (upstream === 'anthropic') {
      const rule = matchRules(RULE_SETS['anthropic'], normalized);
      minTokens = rule ? rule.min_tokens : PROVIDERS['anthropic'].default_min_tokens;
      tierNote = rule
        ? `inherited from Anthropic upstream: ${rule.note}`
        : 'inherited from Anthropic upstream (unknown model — conservative 4,096)';
      warnings.push(
        'Threshold assumes routing to the DIRECT Anthropic API; if OpenRouter routes to ' +
          'a Bedrock host the floor may differ. Pin the provider.',
      );
    } else if (upstream === 'openai') {
      minTokens = 1024;
      tierNote = 'inherited from OpenAI upstream (uniform 1,024)';
    } else if (upstream === 'gemini') {
      const rule = matchRules(RULE_SETS['gemini'], normalized);
      minTokens = rule ? rule.min_tokens : PROVIDERS['gemini'].default_min_tokens;
      tierNote = rule
        ? `inherited from Gemini upstream: ${rule.note}`
        : 'inherited from Gemini upstream (conservative 4,096)';
    } else if (upstream === 'xai') {
      minTokens = null;
      tierNote = 'inherited from xAI upstream (no published threshold)';
    } else {
      minTokens = null;
      tierNote = 'could not detect upstream provider from model name';
      warnings.push('Unknown upstream — threshold/enablement rules could not be inherited.');
    }
    return { spec, provider_key: key, model, min_tokens: minTokens, tier_note: tierNote, warnings, upstream };
  }

  if (key === 'vertex' && normalized.includes('claude')) {
    const rule = matchRules(RULE_SETS['anthropic'], normalized);
    const minTokens = rule ? rule.min_tokens : PROVIDERS['anthropic'].default_min_tokens;
    const tierNote = rule
      ? `Claude-on-Vertex (Anthropic tiers): ${rule.note}`
      : 'Claude-on-Vertex, unknown model — conservative 4,096';
    return {
      spec,
      provider_key: key,
      model,
      min_tokens: minTokens,
      tier_note: tierNote,
      warnings,
      upstream: 'anthropic',
    };
  }

  const rule = matchRules(spec.threshold_rules, normalized);
  let minTokens: number | null;
  let tierNote: string;
  if (rule !== null) {
    minTokens = rule.min_tokens;
    tierNote = rule.note;
  } else {
    minTokens = spec.default_min_tokens;
    tierNote = spec.default_min_note;
    if (spec.threshold_rules.length) {
      warnings.push(
        `Model '${model}' not recognized for ${spec.display} — using the fallback ` +
          `threshold (${minTokens}).`,
      );
    }
  }

  return { spec, provider_key: key, model, min_tokens: minTokens, tier_note: tierNote, warnings, upstream };
}
