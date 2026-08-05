/**
 * cachelint — prompt-cache pre-flight analyzer.
 *
 * Analyzes prompts BEFORE they are sent: 18 dynamic-content detectors, a
 * 7-provider caching knowledge base (thresholds, TTLs, enablement), stable-
 * prefix computation, verdicts, and sequence analysis (predicted HIT /
 * PARTIAL / MISS across consecutive requests).
 *
 * Usage:
 *   import { analyze, renderReport } from 'tryaii/cachelint';
 *
 *   const result = analyze({
 *     prompt: { system: '...', messages: [{ role: 'user', content: '...' }] },
 *     llm: { provider: 'anthropic', name: 'claude-fable-5' },
 *   });
 *   console.log(result.items[0].verdict);
 *   console.log(renderReport(result));
 *
 * Deliberately NOT re-exported from the package root: the tokenizer rank
 * data is multi-MB and router users should never load it. Behavior contract:
 * shared/cachelint/SPEC.md (mirrored by the Python SDK's tryaii.cachelint;
 * both conform to the same golden fixtures).
 */

export { analyze, analyzeFull } from './api.js';
export { analyzeItem, buildCanonical } from './analyzer.js';
export type { AnalyzedItem, Section } from './analyzer.js';
export { render as renderReport } from './report.js';
export { resolve, normModel, PROVIDERS, META } from './providers.js';
export type { ProviderSpec, Resolved, ThresholdRule } from './providers.js';
export { scan, blocking, firstBlocking } from './detectors.js';
export type { Finding } from './detectors.js';
export { countTokens } from './tokenizers.js';
export type { TokenCount } from './tokenizers.js';
