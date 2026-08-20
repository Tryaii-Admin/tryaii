/**
 * Submission assembly (SPEC.md §4). Mirrors designpartner/payload.py.
 *
 * The preview and the sent submission are the SAME document — the preview
 * just has null submitted_at/confirmed_at. Tier decides which diagnose
 * layers are included; the raw-prompt inventory is only ever attached for
 * full_partnership, whose consent copy discloses it in plain words.
 */

export interface BuildSubmissionOptions {
  version: string;
  submittedAt: unknown;
  confirmedAt: unknown;
}

export function buildSubmission(
  state: Record<string, any>,
  diagnoseDocs: Record<string, any> | null,
  opts: BuildSubmissionOptions,
): Record<string, unknown> {
  const consent = state.consent;
  const tier = consent.tier as string;

  let diagnose: Record<string, unknown> | null = null;
  if (tier !== 'contact_only' && diagnoseDocs !== null) {
    if (tier === 'summary_insights') {
      diagnose = { run_id: diagnoseDocs.run_id, summary: diagnoseDocs.summary };
    } else {
      // full_partnership
      diagnose = {
        run_id: diagnoseDocs.run_id,
        summary: diagnoseDocs.summary,
        findings: diagnoseDocs.findings,
        inventory: diagnoseDocs.inventory,
      };
    }
  }

  return {
    schema: 'tryaii.designpartner.submission/1',
    submitted_at: opts.submittedAt,
    tool: { name: 'tryaii', version: opts.version },
    consent: {
      tier,
      chosen_at: consent.chosen_at,
      confirmed_at: opts.confirmedAt,
    },
    answers: state.answers,
    diagnose,
  };
}
