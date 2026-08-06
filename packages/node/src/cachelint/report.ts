/**
 * Plain-text report renderer (78-column, deterministic) — byte-identical to
 * the Python renderer for any analyze() result (the CLI cross-diff test
 * compares the two directly). Wrapping measures code points (SPEC.md §1.1);
 * thousands separators are locale-independent (SPEC.md §1.5).
 */

import { cpLength } from './util/codepoints.js';
import { formatFixed } from './util/halfEven.js';
import { formatThousands } from './util/pyformat.js';

const W = 78;
const SEV_TAG: Record<string, string> = { high: '[HIGH]', medium: '[MED ]', low: '[low ]' };

const VERDICT_LABEL: Record<string, string> = {
  CACHEABLE: 'CACHEABLE',
  CACHEABLE_WITH_ACTION: 'CACHEABLE (action required)',
  CACHEABLE_PREFIX: 'CACHEABLE PREFIX (dynamic tail)',
  BELOW_THRESHOLD: "BELOW THRESHOLD (won't cache)",
  EFFECTIVELY_UNCACHEABLE: 'EFFECTIVELY UNCACHEABLE',
  UNKNOWN_THRESHOLD: 'UNKNOWN THRESHOLD (structural analysis only)',
};

function rule(char = '-'): string {
  return char.repeat(W);
}

function wrap(text: string, indent = 4, width = W): string[] {
  const pad = ' '.repeat(indent);
  const out: string[] = [];
  let cur = '';
  for (const w of text.split(/\s+/).filter(Boolean)) {
    const candidate = cur ? cur + ' ' + w : w;
    if (pad.length + cpLength(candidate) > width && cur) {
      out.push(pad + cur);
      cur = w;
    } else {
      cur = candidate;
    }
  }
  if (cur) out.push(pad + cur);
  return out;
}

function fmtTokens(n: unknown): string {
  return typeof n === 'number' && Number.isInteger(n) ? `~${formatThousands(n)}` : '?';
}

type Dict = Record<string, any>;

export function render(result: Dict): string {
  const lines: string[] = [];
  lines.push(rule('='));
  lines.push(' CACHELINT - prompt-cache pre-flight report');
  lines.push(`   knowledge base: ${result.meta.knowledge_base}`);
  lines.push(rule('='));

  for (const item of result.items as Dict[]) {
    lines.push('');
    let header = ` INPUT #${item.index} -- ${item.provider} / ${item.model || '(no model)'}`;
    if (item.upstream) header += `  [upstream: ${item.upstream}]`;
    lines.push(header);
    lines.push(rule());

    const v = item.verdict;
    lines.push(` Verdict   : ${VERDICT_LABEL[v.code] ?? v.code}`);
    lines.push(...wrap(v.summary, 13));

    const tr = item.token_report;
    const total = tr.total;
    const secs = Object.entries(tr.sections as Dict)
      .map(([name, n]) => `${name}=${fmtTokens(n)}`)
      .join('  ');
    lines.push(` Tokens    : total ${fmtTokens(total.tokens)}  (${total.method})`);
    if (secs) lines.push(...wrap(secs, 13));

    const th = item.threshold;
    if (th.min_tokens !== null) {
      const meets = th.meets ? 'meets' : 'BELOW';
      const inc = th.increment ? `, +${th.increment}-tok steps` : '';
      lines.push(
        ` Threshold : ${formatThousands(th.min_tokens)} min (${th.tier}${inc}) -> prompt ${meets} the floor`,
      );
    } else {
      lines.push(` Threshold : unpublished (${th.tier})`);
    }

    const sr = item.system_report;
    if (sr.present) {
      let unitOk = '';
      if (th.min_tokens !== null) {
        unitOk = sr.cached_unit_meets_threshold
          ? ' -> clears the floor alone'
          : ' -> below the floor alone';
      }
      lines.push(
        ` SystemRpt : system ${fmtTokens(sr.tokens)} tok | cached unit ` +
          `(tools+system) ${fmtTokens(sr.cached_unit_tokens)} tok${unitOk}` +
          (sr.findings_in_system ? ` | ${sr.findings_in_system} finding(s) inside system` : ''),
      );
    }

    const findings = item.findings as Dict[];
    if (findings.length) {
      lines.push(` Findings  : ${findings.length}`);
      for (const f of findings.slice(0, 10)) {
        const tag = SEV_TAG[f.severity] ?? `[${f.severity}]`;
        lines.push(
          `   ${tag} ${f.kind}  @ ${f.section}+${f.section_offset}` +
            `  (${formatFixed(f.pct_into_prompt, 0)}% into prompt)`,
        );
        lines.push(...wrap(`"${f.excerpt}"`, 10));
        lines.push(...wrap('-> ' + f.why, 10));
      }
      if (findings.length > 10) {
        lines.push(`   ... and ${findings.length - 10} more`);
      }
    } else {
      lines.push(' Findings  : none — no dynamic-content indicators detected');
    }

    const sp = item.stable_prefix;
    const lim = sp.limited_by;
    if (lim) {
      lines.push(
        ` StablePfx : ${fmtTokens(sp.tokens)} tok ` +
          `(${sp.pct_of_prompt}% of prompt), capped by ${lim.kind} ` +
          `at ${lim.section}+${lim.section_offset}`,
      );
      if (sp.if_first_fixed_tokens !== null && sp.if_first_fixed_tokens !== undefined) {
        lines.push(
          `             fixing that first finding alone -> ` +
            `${fmtTokens(sp.if_first_fixed_tokens)} tok`,
        );
      }
    } else {
      lines.push(` StablePfx : ${fmtTokens(sp.tokens)} tok (entire prompt is stable)`);
    }

    const en = item.enablement;
    lines.push(` Enablement: ${(en.mode as string).toUpperCase()}`);
    lines.push(...wrap(en.detail, 13));

    if ((item.recommendations as string[]).length) {
      lines.push(' Recommendations:');
      (item.recommendations as string[]).forEach((rec, i) => {
        lines.push(...wrap(`${i + 1}. ${rec}`, 4));
      });
    }

    const intel = item.provider_intel;
    lines.push(' Intel     :');
    lines.push(...wrap(`TTL: ${intel.ttl}`, 6));
    lines.push(...wrap(`Read discount: ${intel.read_discount}`, 6));
    lines.push(...wrap(`Write cost: ${intel.write_cost}`, 6));
    lines.push(...wrap(`Verify via: ${intel.verify_field}`, 6));
    for (const g of intel.gotchas as string[]) {
      lines.push(...wrap(`! ${g}`, 6));
    }

    for (const w of (item.warnings as string[]) ?? []) {
      lines.push(...wrap(`(warning) ${w}`, 2));
    }
  }

  for (const seq of (result.sequences as Dict[]) ?? []) {
    const g = seq.group;
    lines.push('');
    if (seq.count === 0) {
      // cross-group note
      lines.push(rule('='));
      lines.push(...wrap(seq.summary, 1));
      continue;
    }
    lines.push(
      ` SEQUENCE -- ${g.provider} / ${g.model} ` +
        `(${seq.count} request(s): #${(seq.indices as number[]).join(', #')})`,
    );
    lines.push(rule());
    if (seq.shared_prefix_all) {
      const spa = seq.shared_prefix_all;
      const ok = spa.meets_threshold ? 'clears' : 'does NOT clear';
      const floor = spa.min_tokens !== null ? formatThousands(spa.min_tokens) : 'unpublished';
      lines.push(
        ` Shared prefix across all: ${fmtTokens(spa.tokens)} tok ` + `-> ${ok} the floor (${floor})`,
      );
    }
    for (const p of seq.pairs as Dict[]) {
      lines.push(
        ` #${p.from} -> #${p.to}  ${(p.expected as string).padEnd(8)} ` +
          `relation=${p.relation}  common prefix ${fmtTokens(p.lcp_tokens)} tok` +
          (p.expected_cached_tokens ? ` (cached ${fmtTokens(p.expected_cached_tokens)})` : ''),
      );
      const d = p.diverged_at;
      if (d) {
        lines.push(`      diverged at ${d.section}+${d.section_offset} ` + `(char ${d.offset})`);
        if ((p.likely_causes as string[]).length) {
          lines.push(...wrap('likely cause: ' + (p.likely_causes as string[]).join('; '), 6));
        }
        lines.push(...wrap(`prev: "${d.context_prev}"`, 6));
        lines.push(...wrap(`curr: "${d.context_curr}"`, 6));
      }
      const t = p.ttl_check;
      if (t) {
        let gapLine = `gap between requests: ${t.gap_seconds}s`;
        if (t.note) gapLine += ` — ${t.note}`;
        lines.push(...wrap(gapLine, 6));
      }
      for (const n of p.notes as string[]) {
        lines.push(...wrap('note: ' + n, 6));
      }
    }
    lines.push(...wrap('Summary: ' + seq.summary, 1));
  }

  lines.push('');
  lines.push(rule('='));
  lines.push(...wrap('Caveat: ' + result.meta.caveat, 1));
  return lines.join('\n');
}
