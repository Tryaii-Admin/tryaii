/**
 * Custom-registry edges of the SPEC §2.1 alias fallback that the default
 * catalog fixtures can't carry. Mirrors the resolve tests in
 * packages/python/tests/test_diagnose_unit.py.
 */

import { describe, expect, it } from 'vitest';

import { resolveModelId } from '../src/diagnose/index.js';
import { ModelRegistry } from '../src/registry/models.js';

function registryOf(...modelIds: string[]): ModelRegistry {
  const registry = new ModelRegistry();
  for (const modelId of modelIds) registry.add({ modelId, provider: 'TestCo' });
  return registry;
}

describe('resolveModelId alias fallback', () => {
  it('never overrides an exact or tail match', () => {
    const registry = registryOf(
      'anthropic/claude-sonnet-4.5',
      'anthropic/claude-sonnet-4-5-20250929',
      'x-ai/grok-4',
      'x-ai/grok-4-latest',
    );
    expect(resolveModelId('anthropic/claude-sonnet-4-5-20250929', registry)).toEqual([
      'anthropic/claude-sonnet-4-5-20250929',
      'exact',
    ]);
    expect(resolveModelId('claude-sonnet-4-5-20250929', registry)).toEqual([
      'anthropic/claude-sonnet-4-5-20250929',
      'normalized',
    ]);
    expect(resolveModelId('grok-4-latest', registry)).toEqual(['x-ai/grok-4-latest', 'normalized']);
  });

  it('only lands on models in the registry', () => {
    const registry = registryOf('mistralai/mistral-large-2512');
    expect(resolveModelId('mistral-large-latest', registry)).toEqual([null, 'none']);
    expect(resolveModelId('claude-sonnet-4-5-20250929', registry)).toEqual([null, 'none']);
  });

  it('strips one date / -latest suffix and never guesses on ambiguity', () => {
    const registry = registryOf('acme/foo-2', 'acme/foo.3', 'other/foo-3');
    expect(resolveModelId('foo-2-20250101', registry)).toEqual(['acme/foo-2', 'alias']);
    expect(resolveModelId('foo@20250101', registry)).toEqual([null, 'none']);
    expect(resolveModelId('FOO-2-LATEST', registry)).toEqual(['acme/foo-2', 'alias']);
    expect(resolveModelId('foo-3-latest', registry)).toEqual([null, 'none']);
    expect(resolveModelId('foo-2-2025-01-01', registry)).toEqual([null, 'none']);
    expect(resolveModelId('foo-2-12345678', registry)).toEqual([null, 'none']);
  });
});
