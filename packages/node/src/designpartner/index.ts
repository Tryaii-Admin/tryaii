/**
 * designpartner — resumable design-partner enrollment.
 *
 * One command (`tryaii designpartner`) drives questionnaire -> optional
 * diagnose run -> tiered consent -> confirmed submission. Contract:
 * shared/designpartner/SPEC.md. Nothing is sent without --confirm; every
 * submission is saved locally first. Subpath-only export
 * (`tryaii/designpartner`), like the diagnose module.
 */

export {
  advance,
  DEFAULT_DIAGNOSE_OUT_DIR,
  type AdvanceInputs,
  type AdvanceOpts,
  type AdvanceSeams,
} from './api.js';
export {
  allQuestions,
  applicableQuestions,
  loadCatalog,
  tiersById,
  validateAnswers,
} from './catalog.js';
export { buildSubmission } from './payload.js';
export { deriveStage, loadState, PREVIEW_FILE, STATE_FILE } from './state.js';
export { DEFAULT_URL, effectiveUrl, sendSubmission } from './transport.js';
