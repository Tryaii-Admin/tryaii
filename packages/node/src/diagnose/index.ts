/**
 * diagnose — agent-first codebase diagnostics (insight-only).
 *
 * The coding agent discovers LLM call sites and writes an inventory JSON;
 * this module runs the deterministic checks over it (contract:
 * shared/diagnose/SPEC.md) and persists runs under .tryaii/diagnose/.
 *
 * Subpath-only (`tryaii/diagnose`), like `tryaii/cachelint`: it transitively
 * loads the multi-MB tokenizer data, so it must never enter the root barrel.
 */

export {
  analyzeInventory,
  DEFAULT_CHECKS,
  readDiscountFactors,
  type AnalyzeInventoryOpts,
  type ClassifyFn,
} from './engine.js';
export { normalizeInventory, cleanClassification, DEFAULT_OUTPUT_TOKENS } from './intake.js';
export { resolveModelId } from './resolve.js';
export { runModelFit } from './modelfit.js';
export { runCost } from './cost.js';
export { runHygiene } from './hygiene.js';
export {
  writeRun,
  listRunIds,
  latestRunId,
  previousRunId,
  loadRunFindings,
  loadRunInventory,
} from './store.js';
export { renderReportHtml, renderTemplate, buildScope } from './report.js';
