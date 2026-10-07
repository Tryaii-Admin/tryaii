/**
 * Free-tier model exclusion (Node side).
 *
 * Free tiers (OpenRouter `:free` id variants) come and go -- often within
 * days -- so regular routing must ignore them completely, while the full
 * catalog keeps them for the future dedicated free-models feature. The
 * packaged starter catalog ships no free tiers at all; the counts are pinned
 * on the full catalog bundle (skipped when it has not been built).
 * Mirrors packages/python/tests/test_registry/test_free_tier.py.
 */

import { describe, it, expect } from 'vitest';

import { ModelRegistry, isFreeTier } from '../../src/registry/models.js';
import { FULL_ONLY, HAS_FULL_BUNDLE, fullBundle, starterBundle } from '../_catalog.js';

const rawFull = () => fullBundle().modelEntries();

describe('isFreeTier', () => {
  it('detects the :free suffix', () => {
    expect(isFreeTier('openai/gpt-oss-120b:free')).toBe(true);
    expect(isFreeTier('z-ai/glm-4.5-air:free')).toBe(true);
  });

  it('leaves regular ids alone', () => {
    expect(isFreeTier('openai/gpt-5.5')).toBe(false);
    expect(isFreeTier('anthropic/claude-fable-5')).toBe(false);
    expect(isFreeTier('freellm/model')).toBe(false);
    expect(isFreeTier('vendor/free-model')).toBe(false);
  });
});

describe('default registry excludes free tiers', () => {
  it('no free models in regular usage', () => {
    const registry = ModelRegistry.default();
    const free = registry.modelIds.filter(isFreeTier);
    expect(free).toEqual([]);
  });

  it('the starter catalog ships no free tiers', () => {
    expect(starterBundle().modelEntries().some((m) => isFreeTier(m.model_id))).toBe(false);
  });

  it('loadPreset("default") loads the default catalog', () => {
    const registry = new ModelRegistry();
    expect(registry.loadPreset('default')).toBe(starterBundle().modelEntries().length);
  });
});

describe.skipIf(!HAS_FULL_BUNDLE)(`full catalog excludes free tiers by default ${FULL_ONLY}`, () => {
  it('default count is paid-only (322)', () => {
    const paid = rawFull().filter((m) => !isFreeTier(m.model_id));
    expect(ModelRegistry.default(false, fullBundle()).length).toBe(paid.length);
    expect(paid.length).toBe(322);
    expect(ModelRegistry.fromBundle(fullBundle()).modelIds.some(isFreeTier)).toBe(false);
  });
});

describe.skipIf(!HAS_FULL_BUNDLE)(`free data preserved for opt-in ${FULL_ONLY}`, () => {
  it('the full catalog still ships free-tier models', () => {
    const free = rawFull().filter((m) => isFreeTier(m.model_id));
    expect(free.length).toBeGreaterThanOrEqual(10);
  });

  it('includeFree opt-in loads everything', () => {
    const registry = ModelRegistry.default(true, fullBundle());
    expect(registry.length).toBe(rawFull().length);
    expect(registry.length).toBe(362);
    expect(registry.modelIds.some(isFreeTier)).toBe(true);
  });

  it('opt-in free models are fully usable entries', () => {
    const registry = ModelRegistry.fromBundle(fullBundle(), { includeFree: true });
    const free = registry.allModels.filter((m) => isFreeTier(m.modelId));
    expect(free.length).toBeGreaterThan(0);
    for (const m of free) {
      expect(Object.keys(m.benchmarkScores).length, m.modelId).toBeGreaterThan(0);
      expect(m.latency, m.modelId).not.toBeNull();
    }
  });
});
