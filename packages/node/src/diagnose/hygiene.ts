/**
 * hygiene check (SPEC.md §2.5) — provider-independent prompt structure scan.
 * Mirrors diagnose/hygiene.py.
 */

import { hygieneFindings } from '../cachelint/analyzer.js';

export const FINDINGS_EMITTED = 10;
const FIELDS = [
  'kind',
  'severity',
  'section',
  'section_offset',
  'pct_into_prompt',
  'excerpt',
  'why',
  'hint',
] as const;

export const NO_SYSTEM_ADVISORY =
  'No system block: shared static instructions in a system block form the classic cacheable unit.';

export function runHygiene(prompt: unknown): Record<string, unknown> {
  const h = hygieneFindings(prompt) as {
    findings: Record<string, unknown>[];
    blocking_count: number;
    findings_in_system: number;
    has_system: boolean;
    has_tools: boolean;
    message_count: number;
  };

  const advisories: string[] = [];
  const structured = prompt !== null && typeof prompt === 'object' && !Array.isArray(prompt);
  if (structured && !h.has_system && !h.has_tools && h.message_count >= 2) {
    advisories.push(NO_SYSTEM_ADVISORY);
  }
  if (h.findings_in_system > 0) {
    advisories.push(
      `${h.findings_in_system} dynamic value(s) inside the system ` +
        'block — the system block should be fully static.',
    );
  }

  return {
    status: h.blocking_count > 0 ? 'finding' : 'ok',
    reason: null,
    findings_count: h.findings.length,
    blocking_count: h.blocking_count,
    findings_in_system: h.findings_in_system,
    findings: h.findings.slice(0, FINDINGS_EMITTED).map((f) => {
      const projected: Record<string, unknown> = {};
      for (const k of FIELDS) projected[k] = f[k];
      return projected;
    }),
    advisories,
  };
}
