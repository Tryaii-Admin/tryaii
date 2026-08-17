/**
 * Questionnaire catalog: loading, ask_if evaluation, answer validation
 * (shared/designpartner/SPEC.md §3). Mirrors designpartner/catalog.py.
 */

import { readFileSync } from 'node:fs';

let catalogCache: Record<string, any> | null = null;

export function loadCatalog(): Record<string, any> {
  if (catalogCache === null) {
    catalogCache = JSON.parse(
      readFileSync(new URL('./data/questions.json', import.meta.url), 'utf-8'),
    ) as Record<string, any>;
  }
  return catalogCache;
}

/** Flat question list in catalog order. */
export function allQuestions(catalog: Record<string, any>): Record<string, any>[] {
  const out: Record<string, any>[] = [];
  for (const section of catalog.sections) {
    for (const question of section.questions) out.push(question);
  }
  return out;
}

export function tiersById(catalog: Record<string, any>): Record<string, any> {
  const out: Record<string, any> = {};
  for (const tier of catalog.consent_tiers) out[tier.id] = tier;
  return out;
}

/**
 * An answer counts as present when the key exists and the value is not an
 * empty string / empty list (SPEC §3: '' and [] are treated as absent).
 */
function present(answers: Record<string, unknown>, qid: string): boolean {
  if (!(qid in answers)) return false;
  const value = answers[qid];
  if (value === '') return false;
  if (Array.isArray(value) && value.length === 0) return false;
  return true;
}

function conditionMet(
  cond: Record<string, any> | undefined,
  answers: Record<string, unknown>,
): boolean {
  if (cond === undefined) return true;
  const qid = cond.question as string;
  const op = cond.op as string;
  if (op === 'answered') return present(answers, qid);
  if (!present(answers, qid)) return false;
  const value = answers[qid];
  if (op === 'equals') return value === cond.value;
  if (op === 'contains') return Array.isArray(value) && value.includes(cond.value);
  return false;
}

/**
 * Ids of questions whose ask_if is met (single forward pass — a condition
 * may only reference an earlier question, SPEC §3.1).
 */
export function applicableQuestions(
  catalog: Record<string, any>,
  answers: Record<string, unknown>,
): string[] {
  return allQuestions(catalog)
    .filter((q) => conditionMet(q.ask_if, answers))
    .map((q) => q.id as string);
}

/** SPEC §3.2: contains at least one '@' with non-empty text on both sides. */
function validEmail(value: string): boolean {
  const at = value.indexOf('@');
  return at > 0 && at < value.length - 1;
}

/** Problem code for a present answer of the wrong shape, else null. */
function typeProblem(question: Record<string, any>, value: unknown): string | null {
  const qtype = question.type as string;
  if (qtype === 'text' || qtype === 'select') {
    if (typeof value !== 'string') return 'invalid_type';
    if (qtype === 'select') {
      const optionIds = question.options.map((o: Record<string, any>) => o.id as string);
      if (!optionIds.includes(value)) return 'unknown_option';
    } else if (question.format === 'email' && !validEmail(value)) {
      return 'invalid_email';
    }
  } else if (qtype === 'multi_select') {
    if (!Array.isArray(value) || !value.every((v) => typeof v === 'string')) {
      return 'invalid_type';
    }
    const optionIds = question.options.map((o: Record<string, any>) => o.id as string);
    if (value.some((v) => !optionIds.includes(v))) return 'unknown_option';
  } else if (qtype === 'boolean') {
    if (typeof value !== 'boolean') return 'invalid_type';
  }
  return null;
}

/**
 * SPEC §3.2 — returns {answers: canonical, problems, warnings}. ALL
 * problems reported at once; canonical answers are the applicable,
 * present, valid ones in catalog order.
 */
export function validateAnswers(
  catalog: Record<string, any>,
  answers: Record<string, unknown>,
): Record<string, unknown> {
  const questions = allQuestions(catalog);
  const knownIds = new Set(questions.map((q) => q.id as string));

  const canonical: Record<string, unknown> = {};
  const problems: Record<string, string>[] = [];
  const warnings: Record<string, string>[] = [];

  // Single forward pass: conditions are evaluated against the answers as
  // given (a condition may only reference an earlier question).
  for (const question of questions) {
    const qid = question.id as string;
    const applicable = conditionMet(question.ask_if, answers);
    const isPresent = present(answers, qid);

    if (!applicable) {
      if (isPresent) {
        warnings.push({
          question: qid,
          code: 'not_applicable',
          message: `'${qid}' does not apply to these answers — dropped`,
        });
      }
      continue;
    }

    if (!isPresent) {
      if (question.required) {
        problems.push({ question: qid, code: 'missing', message: `'${qid}' is required` });
      }
      continue;
    }

    const code = typeProblem(question, answers[qid]);
    if (code !== null) {
      const messages: Record<string, string> = {
        invalid_type: `'${qid}' has the wrong type for a ${question.type} question`,
        unknown_option: `'${qid}' contains a value that is not one of its option ids`,
        invalid_email: `'${qid}' must look like an email address`,
      };
      problems.push({ question: qid, code, message: messages[code] });
      continue;
    }

    canonical[qid] = answers[qid];
  }

  for (const qid of Object.keys(answers)) {
    if (!knownIds.has(qid)) {
      problems.push({
        question: qid,
        code: 'unknown_question',
        message: `'${qid}' is not in the questionnaire`,
      });
    }
  }

  return { answers: canonical, problems, warnings };
}
