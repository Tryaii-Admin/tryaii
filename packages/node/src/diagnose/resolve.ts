/**
 * Model-id resolution against the routing registry (SPEC.md §2.1).
 *
 * Deterministic, no fuzzy matching: exact id, then OpenRouter slug, then the
 * cachelint normalization rule, then (only when all of those find nothing) the
 * provider-native alias fallback — an unresolved model degrades honestly
 * downstream instead of guessing. Mirrors diagnose/resolve.py.
 */

import { MODEL_ID_TO_OPENROUTER } from '../integrations/openrouter.js';
import type { ModelRegistry } from '../registry/models.js';

/** The cachelint norm_model rule: trim, lowercase, [ ._/]+ runs -> '-'. */
function norm(name: string): string {
  return (name || '').trim().toLowerCase().replace(/[ ._/]+/g, '-');
}

/**
 * A provider-native version suffix: Anthropic-style compact dates
 * ("-20250929", Vertex "@20250929") or a floating "-latest" alias.
 */
const VERSION_SUFFIX = /(?:[-@]20[0-9]{6}|-latest)$/;

export type ResolveMethod = 'exact' | 'slug' | 'normalized' | 'alias' | 'none';

/** Resolve a site's model string to a registry id. */
export function resolveModelId(
  model: string | null | undefined,
  registry: ModelRegistry,
): [string | null, ResolveMethod] {
  if (!model) return [null, 'none'];
  if (registry.getModel(model) !== undefined) return [model, 'exact'];

  const normalized = norm(model);

  // OpenRouter slugs ("anthropic/claude-sonnet-4.5" -> registry id).
  for (const [modelId, slug] of Object.entries(MODEL_ID_TO_OPENROUTER)) {
    if (norm(slug) === normalized && registry.getModel(modelId) !== undefined) {
      return [modelId, 'slug'];
    }
  }

  // Normalized comparison against every registry id; ambiguity resolves to
  // nothing (SPEC.md §2.1 — never guess between two models).
  let candidates = registry.modelIds.filter((mid) => norm(mid) === normalized);
  if (candidates.length === 1) return [candidates[0], 'normalized'];
  if (candidates.length > 1) return [null, 'none'];

  // Last try: compare the text after the final '/' on BOTH sides, so a bare
  // "gpt-4o" finds the provider-prefixed "openai/gpt-4o" and "myorg/gpt-4o"
  // does too. Ambiguity still resolves to nothing.
  const tail = norm(model.slice(model.lastIndexOf('/') + 1));
  candidates = registry.modelIds.filter(
    (mid) => norm(mid.slice(mid.lastIndexOf('/') + 1)) === tail,
  );
  if (candidates.length === 1) return [candidates[0], 'normalized'];
  if (candidates.length > 1) return [null, 'none'];

  // Alias fallback for provider-native API ids the OpenRouter-style catalog
  // doesn't carry ("claude-sonnet-4-5-20250929", "grok-4-latest"). Never
  // reached when an earlier step matched; only ever lands on a model that is
  // in THIS registry.
  const rawTail = model.slice(model.lastIndexOf('/') + 1).trim();
  // (a) the SDK's provider-native id -> OpenRouter slug table.
  for (const [nativeId, slug] of Object.entries(MODEL_ID_TO_OPENROUTER)) {
    if (norm(nativeId) === tail && registry.getModel(slug) !== undefined) {
      return [slug, 'alias'];
    }
  }
  // (b) drop ONE trailing date / "-latest" suffix and retry the tail rule.
  const lowered = rawTail.toLowerCase();
  const stripped = lowered.replace(VERSION_SUFFIX, '');
  if (stripped && stripped !== lowered) {
    const base = norm(stripped);
    candidates = registry.modelIds.filter(
      (mid) => norm(mid.slice(mid.lastIndexOf('/') + 1)) === base,
    );
    if (candidates.length === 1) return [candidates[0], 'alias'];
  }

  return [null, 'none'];
}
