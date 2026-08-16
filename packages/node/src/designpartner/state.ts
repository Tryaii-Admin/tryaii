/**
 * Enrollment state: load/save + stage derivation (SPEC.md §2, §4).
 * Mirrors designpartner/state.py.
 */

import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';

export const STATE_FILE = 'state.json';
export const PREVIEW_FILE = 'preview.json';

export const STAGES = ['questionnaire', 'diagnose', 'consent', 'confirm', 'submitted'];

export function newState(now: unknown, version: string): Record<string, any> {
  return {
    schema: 'tryaii.designpartner.state/1',
    created_at: now,
    updated_at: now,
    tool: { name: 'tryaii', version },
    stage: 'questionnaire',
    answers: null,
    consent: null,
    submission: null,
  };
}

export function loadState(outDir: string): Record<string, any> | null {
  const path = join(outDir, STATE_FILE);
  if (!existsSync(path)) return null;
  return JSON.parse(readFileSync(path, 'utf-8')) as Record<string, any>;
}

export function saveState(outDir: string, state: Record<string, any>): string {
  const path = join(outDir, STATE_FILE);
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify(state, null, 2) + '\n', 'utf-8');
  return path;
}

/** SPEC §2 — answers -> consent -> (diagnose gate) -> confirm -> submitted. */
export function deriveStage(
  state: Record<string, any>,
  hasDiagnoseRun: boolean,
  tiers: Record<string, any>,
): string {
  if (state.answers === null) return 'questionnaire';
  if (state.consent === null) return 'consent';
  const tier = tiers[state.consent.tier];
  if (tier.requires_diagnose && !hasDiagnoseRun) return 'diagnose';
  if (state.submission === null) return 'confirm';
  return 'submitted';
}
