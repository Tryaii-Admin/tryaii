/**
 * HTML report renderer (SPEC.md §4) — byte-for-byte mirror of report.py.
 *
 * One shared template (data/report_template.json) + a two-construct
 * substitution language. Every scope value is a pre-formatted STRING, and a
 * missing template key is an ERROR — template and scope builder cannot
 * drift apart silently (the eval dashboard's cross-SDK drift is the
 * cautionary tale).
 */

import { readFileSync } from 'node:fs';

import { formatFixed } from '../cachelint/util/halfEven.js';

type Scope = Record<string, unknown>;

let templateCache: string | null = null;

export const CHECK_LABELS: Record<string, string> = {
  model_fit: 'model fit',
  cache_readiness: 'cache readiness',
  cost_exposure: 'cost exposure',
  hygiene: 'hygiene',
};
const BADGES: Record<string, string> = {
  ok: 'ok',
  finding: 'finding',
  insufficient_data: 'insufficient',
  skipped: 'skipped',
};

function loadTemplate(): string {
  if (templateCache === null) {
    templateCache = (
      JSON.parse(
        readFileSync(new URL('./data/report_template.json', import.meta.url), 'utf-8'),
      ) as { html: string }
    ).html;
  }
  return templateCache;
}

// ---------------------------------------------------------------------------
// Substitution engine (SPEC §4.1)
// ---------------------------------------------------------------------------

function esc(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function lookup(chain: Scope[], key: string): unknown {
  for (const scope of chain) {
    if (key in scope) return scope[key];
  }
  throw new Error(`template key '${key}' missing from scope`);
}

function subScalars(text: string, chain: Scope[]): string {
  return text.replace(/\{\{([a-z_]+)\}\}/g, (_m, key: string) => {
    const value = lookup(chain, key);
    if (typeof value !== 'string') {
      throw new Error(`template key '${key}' is not a string`);
    }
    return esc(value);
  });
}

export function renderTemplate(template: string, scope: Scope): string {
  return renderChain(template, [scope]);
}

function renderChain(template: string, chain: Scope[]): string {
  const out: string[] = [];
  let pos = 0;
  for (;;) {
    const begin = template.indexOf('<!--BEGIN ', pos);
    if (begin === -1) {
      out.push(subScalars(template.slice(pos), chain));
      break;
    }
    out.push(subScalars(template.slice(pos, begin), chain));
    const nameEnd = template.indexOf('-->', begin);
    const name = template.slice(begin + 10, nameEnd);
    const endMarker = `<!--END ${name}-->`;
    const end = template.indexOf(endMarker, nameEnd);
    if (end === -1) throw new Error(`template block '${name}' has no END marker`);
    const inner = template.slice(nameEnd + 3, end);
    const items = lookup(chain, name);
    if (!Array.isArray(items)) throw new Error(`template block '${name}' is not a list`);
    for (const item of items) {
      out.push(renderChain(inner, [item as Scope, ...chain]));
    }
    pos = end + endMarker.length;
  }
  return out.join('');
}

// ---------------------------------------------------------------------------
// Formatting helpers (SPEC §4.1 — mirrors of report.py)
// ---------------------------------------------------------------------------

function money2(x: number): string {
  return '$' + formatFixed(x, 2);
}

function money4(x: number): string {
  return '$' + formatFixed(x, 4);
}

function signedMoney2(x: number): string {
  const sign = x < 0 ? '-' : '+';
  return sign + '$' + formatFixed(Math.abs(x), 2);
}

/** Stringify a §1.3-normalized number (or null -> 'n/a'). */
function s(value: unknown): string {
  return value === null || value === undefined ? 'n/a' : String(value);
}

// ---------------------------------------------------------------------------
// Scope builder
// ---------------------------------------------------------------------------

function deltaScope(findings: Record<string, any>, previous: Record<string, any>): Scope {
  const prevSites = new Map<string, any>(
    previous.sites.map((site: any) => [site.site_id, site]),
  );
  const curSites = new Map<string, any>(
    findings.sites.map((site: any) => [site.site_id, site]),
  );

  let improved = 0;
  let regressed = 0;
  for (const [siteId, cur] of curSites) {
    const prev = prevSites.get(siteId);
    if (prev === undefined) continue;
    let ups = 0;
    let downs = 0;
    for (const check of Object.keys(CHECK_LABELS)) {
      const a = prev.checks[check].status as string;
      const b = cur.checks[check].status as string;
      if (['ok', 'finding'].includes(a) && ['ok', 'finding'].includes(b)) {
        if (a === 'finding' && b === 'ok') ups += 1;
        else if (a === 'ok' && b === 'finding') downs += 1;
      }
    }
    if (downs) regressed += 1;
    else if (ups) improved += 1;
  }

  let added = 0;
  for (const siteId of curSites.keys()) if (!prevSites.has(siteId)) added += 1;
  let removed = 0;
  for (const siteId of prevSites.keys()) if (!curSites.has(siteId)) removed += 1;

  const monthly: Scope[] = [];
  const curTotal = findings.summary.totals.est_monthly_cost_usd;
  const prevTotal = previous.summary.totals.est_monthly_cost_usd;
  if (curTotal !== null && prevTotal !== null) {
    monthly.push({ value: signedMoney2(curTotal - prevTotal) });
  }

  return {
    prev_run_id: previous.run_id,
    improved: String(improved),
    regressed: String(regressed),
    added: String(added),
    removed: String(removed),
    monthly,
  };
}

function modelFitScope(payload: Record<string, any>): Scope[] {
  if (!('top' in payload)) return [];
  const cls = payload.classification;
  const broad = cls.broad_category;
  const sub = cls.subcategory;
  const category = broad !== null && sub !== null ? `${broad} > ${sub}` : 'n/a';
  const currentRank = payload.current_rank;

  const rows: Scope[] = payload.top.map((entry: any) => ({
    rank: String(entry.rank),
    model_id: entry.model_id,
    cls: entry.rank === currentRank ? 'current' : '',
    q: s(entry.quality_score),
    c: s(entry.cost_score),
    s: s(entry.speed_score),
    reasoning: entry.reasoning,
  }));
  // The user's model always appears, even when it ranks below the top 5.
  if (currentRank !== null && currentRank > payload.top.length) {
    const current = payload.current;
    rows.push({
      rank: String(currentRank),
      model_id: current.model_id,
      cls: 'current',
      q: s(current.quality_score),
      c: s(current.cost_score),
      s: s(current.speed_score),
      reasoning: '',
    });
  }

  return [
    {
      summary: payload.summary,
      category,
      confidence: s(cls.confidence),
      rows,
    },
  ];
}

function cacheScope(payload: Record<string, any>): Scope[] {
  if (!('verdict' in payload)) return [];
  const prefix = payload.stable_prefix;
  const findingsItems: Scope[] = payload.top_findings.map((f: any) => ({
    kind: f.kind,
    where: `${f.section}+${f.section_offset}`,
    excerpt: f.excerpt,
    why: f.why,
  }));
  const recsItems: Scope[] = payload.recommendations.map((text: string) => ({ text }));
  return [
    {
      verdict_code: payload.verdict.code,
      verdict_summary: payload.verdict.summary,
      total_tokens: s(payload.total_tokens),
      threshold: s(payload.threshold_min_tokens),
      stable_tokens: s(prefix.tokens),
      stable_pct: s(prefix.pct_of_prompt),
      bar_width: s(prefix.pct_of_prompt),
      findings: findingsItems.length ? [{ items: findingsItems }] : [],
      recs: recsItems.length ? [{ items: recsItems }] : [],
    },
  ];
}

function costScope(payload: Record<string, any>): Scope[] {
  if (!['ok', 'finding'].includes(payload.status)) return [];
  const rows: Scope[] = [
    { label: 'input tokens', value: `${payload.input_tokens} (${payload.token_method})` },
    { label: 'output tokens', value: s(payload.output_tokens) },
    { label: 'cost per call', value: money4(payload.cost_per_call_usd) },
  ];
  const monthly = payload.monthly;
  if (monthly.status === 'ok') {
    rows.push({
      label: `monthly cost (${s(monthly.calls_per_day)} calls/day)`,
      value: money2(monthly.cost_usd),
    });
    if ('cache_savings_usd' in monthly) {
      rows.push({ label: 'cache savings (max)', value: money2(monthly.cache_savings_usd) });
    }
  } else {
    rows.push({ label: 'monthly', value: `insufficient data: ${monthly.reason}` });
  }
  const swap = payload.swap;
  if (swap !== null) {
    let value = `${swap.model_id} at ${money4(swap.cost_per_call_usd)}/call`;
    if ('monthly_savings_usd' in swap) {
      value += ` (saves ${money2(swap.monthly_savings_usd)}/mo)`;
    }
    rows.push({ label: 'cheaper swap', value });
  } else {
    rows.push({ label: 'cheaper swap', value: 'none within quality tolerance' });
  }
  return [{ rows }];
}

function hygieneScope(payload: Record<string, any>): Scope[] {
  if (!['ok', 'finding'].includes(payload.status)) return [];
  const findingsItems: Scope[] = payload.findings.map((f: any) => ({
    kind: f.kind,
    where: `${f.section}+${f.section_offset}`,
    excerpt: f.excerpt,
    hint: f.hint,
  }));
  const advisoryItems: Scope[] = payload.advisories.map((text: string) => ({ text }));
  return [
    {
      stats:
        `${payload.findings_count} finding(s), ` +
        `${payload.blocking_count} blocking, ` +
        `${payload.findings_in_system} in system`,
      findings: findingsItems.length ? [{ items: findingsItems }] : [],
      advisories: advisoryItems.length ? [{ items: advisoryItems }] : [],
    },
  ];
}

function siteScope(site: Record<string, any>): Scope {
  const checks = site.checks;
  const chips: Scope[] = [];
  const reasons: Scope[] = [];
  for (const [check, label] of Object.entries(CHECK_LABELS)) {
    const payload = checks[check];
    chips.push({ cls: BADGES[payload.status], label, badge: BADGES[payload.status] });
    if (payload.status === 'insufficient_data') {
      reasons.push({ label, text: payload.reason });
    }
  }
  return {
    site_id: site.site_id,
    line: String(site.line),
    model_label: site.model !== null ? site.model : 'none declared',
    chips,
    reasons,
    mf: modelFitScope(checks.model_fit),
    cache: cacheScope(checks.cache_readiness),
    cost: costScope(checks.cost_exposure),
    hyg: hygieneScope(checks.hygiene),
  };
}

export function buildScope(
  findings: Record<string, any>,
  previous: Record<string, any> | null = null,
): Scope {
  const summary = findings.summary;
  const totals = summary.totals;

  let findingsTotal = 0;
  for (const counts of Object.values(summary.check_status_counts) as Array<
    Record<string, number>
  >) {
    findingsTotal += counts.finding;
  }

  const skippedItems = findings.inventory.skipped as Array<Record<string, any>>;
  const skipped: Scope[] = [];
  if (skippedItems.length) {
    skipped.push({
      count: String(skippedItems.length),
      items: skippedItems.map((item) => ({
        index: String(item.index),
        reason: item.reason,
      })),
    });
  }

  const statusRows: Scope[] = Object.entries(summary.check_status_counts).map(
    ([check, counts]: [string, any]) => ({
      check,
      ok: String(counts.ok),
      finding: String(counts.finding),
      insufficient: String(counts.insufficient_data),
      skipped: String(counts.skipped),
    }),
  );

  const files: Scope[] = [];
  const byFile = new Map<string, { file: string; sites: Scope[] }>();
  for (const site of findings.sites) {
    let group = byFile.get(site.file);
    if (group === undefined) {
      group = { file: site.file, sites: [] };
      byFile.set(site.file, group);
      files.push(group);
    }
    group.sites.push(siteScope(site));
  }

  const goal = findings.interview.goal;
  const priorities = findings.interview.priorities;

  const tile = (value: number | null): string => (value === null ? 'n/a' : money2(value));

  return {
    site_count: String(summary.site_count),
    run_id: findings.run_id,
    generated_at: s(findings.generated_at),
    tool_version: findings.tool.version,
    q: String(priorities.quality),
    c: String(priorities.cost),
    s: String(priorities.speed),
    findings_total: String(findingsTotal),
    findings_cls: findingsTotal > 0 ? ' warn' : '',
    est_cost: tile(totals.est_monthly_cost_usd),
    cache_savings: tile(totals.est_monthly_cache_savings_usd),
    cache_savings_cls: totals.est_monthly_cache_savings_usd !== null ? ' ok' : '',
    swap_savings: tile(totals.est_monthly_swap_savings_usd),
    swap_savings_cls: totals.est_monthly_swap_savings_usd !== null ? ' ok' : '',
    goal: goal !== null ? [{ text: goal }] : [],
    delta: previous !== null ? [deltaScope(findings, previous)] : [],
    skipped,
    status_rows: statusRows,
    files,
  };
}

/** findings (+ optional previous run's findings) -> self-contained HTML. */
export function renderReportHtml(
  findings: Record<string, unknown>,
  previous: Record<string, unknown> | null = null,
): string {
  return renderTemplate(
    loadTemplate(),
    buildScope(findings as Record<string, any>, previous as Record<string, any> | null),
  );
}
