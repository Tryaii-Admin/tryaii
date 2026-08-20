/**
 * The resumable orchestrator: one advance() per CLI invocation
 * (SPEC.md §2, §4). Mirrors designpartner/api.py.
 *
 * Clock-free and network-seamed: now/stamp/version arrive via opts, the
 * diagnose-store reader and the sender are injectable. Usage-level
 * mistakes throw (the CLI maps them to exit 2); everything else reports a
 * stage and exits 0 — enrollment never blocks.
 */

import { existsSync, mkdirSync, readFileSync, unlinkSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';

import { effectiveUrl, sendSubmission } from './transport.js';
import {
  allQuestions,
  applicableQuestions,
  loadCatalog,
  tiersById,
  validateAnswers,
} from './catalog.js';
import { buildSubmission } from './payload.js';
import { PREVIEW_FILE, deriveStage, loadState, newState, saveState } from './state.js';

export const DEFAULT_DIAGNOSE_OUT_DIR = '.tryaii/diagnose';

const NEXT: Record<string, { description: string; command: string | null }> = {
  questionnaire: {
    description:
      'Interview the user from the questionnaire block ' +
      '(honor ask_if; every answer comes from the user), ' +
      'then save their answers.',
    command: 'tryaii designpartner --answers answers.json',
  },
  diagnose: {
    description:
      'The chosen tier requires a diagnose run. Follow the ' +
      "tryaii-diagnose skill (or run 'tryaii diagnose " +
      "check'), then re-run designpartner.",
    command: 'tryaii designpartner',
  },
  consent: {
    description:
      "Show the user each consent tier's copy VERBATIM; " + 'they choose one explicitly.',
    command: 'tryaii designpartner --consent <tier_id>',
  },
  confirm: {
    description:
      'Show the user the preview (exactly what will be ' +
      'sent). Only after they explicitly agree, confirm.',
    command: 'tryaii designpartner --confirm',
  },
  submitted: {
    description: 'Enrollment complete. Nothing further to do.',
    command: null,
  },
};

export type LoadLatestDiagnose = () => Record<string, any> | null;
export type Send = (url: string, body: string) => Promise<boolean> | boolean;

async function defaultLoadLatestDiagnose(
  diagnoseOutDir: string,
): Promise<Record<string, any> | null> {
  const store = await import('../diagnose/store.js');
  const runId = store.latestRunId(diagnoseOutDir);
  if (runId === null) return null;
  const findings = store.loadRunFindings(diagnoseOutDir, runId) as Record<string, any>;
  return {
    run_id: runId,
    summary: findings.summary,
    findings,
    inventory: store.loadRunInventory(diagnoseOutDir, runId),
  };
}

function displayPath(outDir: string, name: string): string {
  return `${outDir}/${name}`.replace(/\\/g, '/');
}

function writeDoc(path: string, doc: Record<string, unknown>): void {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify(doc, null, 2) + '\n', 'utf-8');
}

export interface AdvanceInputs {
  answers?: Record<string, unknown> | null;
  consent?: string | null;
  confirm?: boolean;
  reset?: boolean;
}

export interface AdvanceOpts {
  now?: string | null;
  stamp?: string | null;
  version?: string;
  url?: string;
}

export interface AdvanceSeams {
  diagnoseOutDir?: string;
  loadLatestDiagnose?: LoadLatestDiagnose;
  send?: Send;
}

/**
 * One invocation: ingest inputs, advance state, return the status report
 * (tryaii.designpartner.status/1).
 */
export async function advance(
  outDir: string,
  inputs: AdvanceInputs,
  opts: AdvanceOpts,
  seams: AdvanceSeams = {},
): Promise<Record<string, any>> {
  const catalog = loadCatalog();
  const tiers = tiersById(catalog);
  const now = opts.now ?? null;
  const version = opts.version || '0.0.0';
  const url = opts.url || effectiveUrl();
  const diagnoseOutDir = seams.diagnoseOutDir ?? DEFAULT_DIAGNOSE_OUT_DIR;

  const loadLatest: () => Promise<Record<string, any> | null> = seams.loadLatestDiagnose
    ? async () => (seams.loadLatestDiagnose as LoadLatestDiagnose)()
    : () => defaultLoadLatestDiagnose(diagnoseOutDir);
  const send: Send = seams.send ?? ((target, body) => sendSubmission(target, body, version));

  let action: Record<string, any>;

  // --- reset -----------------------------------------------------------
  if (inputs.reset) {
    const removed: string[] = [];
    for (const name of ['state.json', PREVIEW_FILE]) {
      const path = join(outDir, name);
      if (existsSync(path)) {
        unlinkSync(path);
        removed.push(displayPath(outDir, name));
      }
    }
    action = { type: 'reset', removed };
    return {
      schema: 'tryaii.designpartner.status/1',
      stage: 'questionnaire',
      updated_at: now,
      tool: { name: 'tryaii', version },
      action,
      next: { description: 'Start over from the beginning.', command: 'tryaii designpartner' },
    };
  }

  // --- load / enroll ---------------------------------------------------
  let state = loadState(outDir);
  const isNew = state === null;
  if (state === null) state = newState(now, version);

  // --- ingest the action input ----------------------------------------
  if (inputs.answers !== undefined && inputs.answers !== null) {
    const result = validateAnswers(catalog, inputs.answers) as {
      answers: Record<string, unknown>;
      problems: unknown[];
      warnings: unknown[];
    };
    if (result.problems.length) {
      action = { type: 'answers_rejected', problems: result.problems, warnings: result.warnings };
    } else {
      state.answers = result.answers;
      action = {
        type: 'answers_saved',
        count: Object.keys(result.answers).length,
        warnings: result.warnings,
      };
    }
  } else if (inputs.consent !== undefined && inputs.consent !== null) {
    const tierId = inputs.consent;
    if (!(tierId in tiers)) {
      throw new Error(
        `unknown consent tier: ${tierId}. Valid tiers: ` +
          catalog.consent_tiers.map((t: Record<string, any>) => t.id).join(', '),
      );
    }
    const docs = await loadLatest();
    state.consent = {
      tier: tierId,
      chosen_at: now,
      diagnose_run_id: docs !== null ? docs.run_id : null,
    };
    // A new consent cycle clears the stored submission block (the
    // submission FILES on disk are kept as records).
    state.submission = null;
    action = { type: 'consent_chosen', tier: tierId };
  } else if (inputs.confirm) {
    action = await confirm(state, tiers, outDir, url, now, version, opts, loadLatest, send);
  } else if (isNew) {
    action = { type: 'enrolled' };
  } else {
    action = { type: 'status' };
  }

  // --- derive stage + refresh the preview ------------------------------
  const docs = await loadLatest();
  const hasRun = docs !== null;
  const stage = deriveStage(state, hasRun, tiers);
  state.stage = stage;
  state.updated_at = now;
  saveState(outDir, state);

  let previewBlock: Record<string, unknown> | null = null;
  if (stage === 'confirm') {
    // Regenerated on EVERY entry — the preview can never be stale when
    // shown (SPEC §2).
    const tier = tiers[state.consent.tier];
    const previewDocs = state.consent.tier === 'contact_only' ? null : docs;
    const preview = buildSubmission(state, previewDocs, {
      version,
      submittedAt: null,
      confirmedAt: null,
    });
    writeDoc(join(outDir, PREVIEW_FILE), preview);
    previewBlock = {
      path: displayPath(outDir, PREVIEW_FILE),
      tier: state.consent.tier,
      includes: tier.includes,
      answers_count: Object.keys(state.answers).length,
      diagnose_run_id: docs !== null ? docs.run_id : null,
    };
  }

  // --- report ----------------------------------------------------------
  const report: Record<string, any> = {
    schema: 'tryaii.designpartner.status/1',
    stage,
    updated_at: now,
    tool: { name: 'tryaii', version },
    action,
    next: { ...NEXT[stage] },
  };
  if (stage === 'questionnaire') {
    const answers = state.answers ?? {};
    const applicable = applicableQuestions(catalog, answers);
    const applicableSet = new Set(applicable);
    report.questionnaire = {
      sections: catalog.sections,
      applicable,
      required: allQuestions(catalog)
        .filter((q) => q.required && applicableSet.has(q.id as string))
        .map((q) => q.id as string),
      answers,
    };
  }
  if (stage === 'consent' || stage === 'diagnose') {
    report.consent = {
      tiers: catalog.consent_tiers,
      chosen: state.consent !== null ? state.consent.tier : null,
      requires_diagnose_unmet: stage === 'diagnose',
    };
  }
  if (stage === 'confirm') {
    report.preview = previewBlock;
  }
  if (stage === 'submitted') {
    const submission = state.submission;
    report.submission = {
      path: submission.path,
      delivered: submission.delivered,
      url: submission.url,
      submitted_at: submission.submitted_at,
    };
  }
  return report;
}

/** --confirm: freshness-guarded build + save + send (SPEC §2, §5). */
async function confirm(
  state: Record<string, any>,
  tiers: Record<string, any>,
  outDir: string,
  url: string,
  now: unknown,
  version: string,
  opts: AdvanceOpts,
  loadLatest: () => Promise<Record<string, any> | null>,
  send: Send,
): Promise<Record<string, any>> {
  const docs = await loadLatest();
  const stage = deriveStage(state, docs !== null, tiers);
  if (stage !== 'confirm') {
    throw new Error("nothing to confirm — run 'tryaii designpartner' to see the current stage");
  }

  const previewPath = join(outDir, PREVIEW_FILE);
  if (!existsSync(previewPath)) {
    throw new Error(
      "nothing to confirm — run 'tryaii designpartner' to generate the preview first",
    );
  }
  const onDisk = JSON.parse(readFileSync(previewPath, 'utf-8'));
  const tierId = state.consent.tier as string;
  const expectedDocs = tierId === 'contact_only' ? null : docs;
  const expected = buildSubmission(state, expectedDocs, {
    version,
    submittedAt: null,
    confirmedAt: null,
  });
  if (JSON.stringify(onDisk) !== JSON.stringify(expected)) {
    throw new Error("preview is stale — run 'tryaii designpartner' to regenerate it");
  }

  const stamp = opts.stamp || 'submission';
  const submission = buildSubmission(state, expectedDocs, {
    version,
    submittedAt: now,
    confirmedAt: now,
  });
  const name = `submission-${stamp}.json`;
  writeDoc(join(outDir, name), submission);
  const body = readFileSync(join(outDir, name), 'utf-8');
  const delivered = Boolean(await send(url, body));

  state.submission = {
    stamp,
    submitted_at: now,
    delivered,
    url,
    path: displayPath(outDir, name),
  };
  return { type: 'submitted', delivered };
}
