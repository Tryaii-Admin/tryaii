/**
 * Model-id resolution against the routing registry (SPEC.md §2.1).
 *
 * Deterministic, no fuzzy matching: exact id, then OpenRouter slug, then the
 * cachelint normalization rule — an unresolved model degrades honestly
 * downstream instead of guessing. Mirrors diagnose/resolve.py.
 */

import { MODEL_ID_TO_OPENROUTER } from '../integrations/openrouter.js';
import type { ModelRegistry } from '../registry/models.js';

/** The cachelint norm_model rule: trim, lowercase, [ ._/]+ runs -> '-'. */
function norm(name: string): string {
  return (name || '').trim().toLowerCase().replace(/[ ._/]+/g, '-');
}

export type ResolveMethod = 'exact' | 'slug' | 'normalized' | 'none';

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

  // Last try: the text after the final '/' (provider-prefixed ids).
  if (model.includes('/')) {
    const tail = norm(model.slice(model.lastIndexOf('/') + 1));
    candidates = registry.modelIds.filter((mid) => norm(mid) === tail);
    if (candidates.length === 1) return [candidates[0], 'normalized'];
  }

  return [null, 'none'];
}
