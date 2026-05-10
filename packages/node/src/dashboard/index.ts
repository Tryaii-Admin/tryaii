/**
 * Self-contained HTML dashboard for an eval run.
 *
 * Reads the same shape that gets written to `summary.json`, so it can be used
 * both at the end of a live run and as a backfill over existing runs.
 */

export interface DashboardSummary {
  totalPrompts: number;
  successCount: number;
  errorCount: number;
  distinctModels: number;
  avgRouteMs: number;
  totalRouteMs: number;
  priorities: { quality: number; cost: number; speed: number };
  distribution: Array<{ model: string; count: number; pct: number }>;
  byCategory: Array<{
    category: string;
    count: number;
    topModels: Array<{ model: string; count: number; pct: number }>;
    topBenchmarks: Array<{ name: string; avgScore: number }>;
  }>;
}

function esc(s: string): string {
  return s.replace(/[&<>"']/g, (c) =>
    c === '&' ? '&amp;' : c === '<' ? '&lt;' : c === '>' ? '&gt;' : c === '"' ? '&quot;' : '&#39;',
  );
}

/**
 * Override the relative `summary.json` / `results.jsonl` footer links with
 * absolute URLs. Needed when the dashboard is rendered to a directory
 * (e.g. system temp) that doesn't sit next to the artifacts.
 */
export interface DashboardLinks {
  summaryHref?: string;
  resultsHref?: string;
}

export function renderDashboard(
  summary: DashboardSummary,
  inputPath: string,
  links: DashboardLinks = {},
): string {
  const generatedAt = new Date().toISOString();
  const { quality, cost, speed } = summary.priorities;
  const summaryHref = links.summaryHref ?? 'summary.json';
  const resultsHref = links.resultsHref ?? 'results.jsonl';

  const priorityChip = (label: string, value: number): string =>
    `<span class="chip chip-p${value}">${label} <b>${value}</b></span>`;

  const distRows = summary.distribution
    .map(
      (row) => `
        <li class="row">
          <span class="row-label" title="${esc(row.model)}">${esc(row.model)}</span>
          <span class="row-bar"><span class="row-bar-fill" style="width:${row.pct}%"></span></span>
          <span class="row-num">${row.count}</span>
          <span class="row-pct">${row.pct}%</span>
        </li>`,
    )
    .join('');

  const categoryCards = summary.byCategory
    .map((cat) => {
      const models = cat.topModels
        .slice(0, 3)
        .map(
          (m) => `
            <li class="row">
              <span class="row-label" title="${esc(m.model)}">${esc(m.model)}</span>
              <span class="row-bar"><span class="row-bar-fill" style="width:${m.pct}%"></span></span>
              <span class="row-pct">${m.pct}%</span>
            </li>`,
        )
        .join('');

      const benches = cat.topBenchmarks
        .slice(0, 5)
        .map((b) => `<li><span>${esc(b.name)}</span><b>${b.avgScore.toFixed(3)}</b></li>`)
        .join('');

      return `
        <article class="card">
          <header class="card-head">
            <h3>${esc(cat.category)}</h3>
            <span class="muted">${cat.count} prompts</span>
          </header>
          <h4 class="card-sub">Top models</h4>
          <ul class="rows">${models}</ul>
          ${benches ? `<h4 class="card-sub">Top benchmarks</h4><ul class="benches">${benches}</ul>` : ''}
        </article>`;
    })
    .join('');

  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>tryaii-dre eval — ${summary.totalPrompts} prompts</title>
<style>
  :root {
    --bg: #0b0d10;
    --panel: #14181d;
    --panel-2: #1b2026;
    --text: #e6e9ee;
    --muted: #8a939d;
    --line: #232932;
    --accent: #6ee7b7;
    --accent-2: #93c5fd;
    --warn: #fcd34d;
  }
  @media (prefers-color-scheme: light) {
    :root { --bg:#fafbfc; --panel:#ffffff; --panel-2:#f4f6f9; --text:#0f1419; --muted:#5b6470; --line:#e6eaef; --accent:#059669; --accent-2:#2563eb; --warn:#b45309; }
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: var(--bg); color: var(--text);
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
  main { max-width: 1100px; margin: 0 auto; padding: 32px 24px 64px; }
  header.top { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 8px; }
  header.top h1 { font-size: 18px; margin: 0; font-weight: 600; letter-spacing: 0.2px; }
  header.top h1 small { color: var(--muted); font-weight: 400; margin-left: 8px; }
  .meta { color: var(--muted); font-size: 12px; }
  .chips { display: flex; gap: 8px; margin: 16px 0 28px; flex-wrap: wrap; }
  .chip { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 999px;
    background: var(--panel-2); border: 1px solid var(--line); font-size: 12px; color: var(--muted); }
  .chip b { color: var(--text); font-weight: 600; }
  .chip-p5 b { color: var(--accent); }
  .chip-p4 b { color: var(--accent-2); }
  .chip-p1 b, .chip-p2 b { color: var(--muted); }
  .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 28px; }
  .stat { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; }
  .stat .k { font-size: 11px; text-transform: uppercase; letter-spacing: 0.6px; color: var(--muted); }
  .stat .v { font-size: 22px; font-weight: 600; margin-top: 4px; }
  .stat .v.warn { color: var(--warn); }
  section { margin-bottom: 32px; }
  section > h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.8px; color: var(--muted);
    font-weight: 600; margin: 0 0 12px; }
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 16px 20px; }
  ul.rows { list-style: none; margin: 0; padding: 0; }
  ul.rows .row { display: grid; grid-template-columns: 1fr 2fr auto auto; gap: 12px; align-items: center;
    padding: 6px 0; border-bottom: 1px dashed var(--line); }
  ul.rows .row:last-child { border-bottom: 0; }
  .row-label { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .row-bar { background: var(--panel-2); border-radius: 4px; height: 8px; overflow: hidden; }
  .row-bar-fill { display: block; height: 100%; background: linear-gradient(90deg, var(--accent), var(--accent-2)); }
  .row-num { color: var(--muted); font-variant-numeric: tabular-nums; min-width: 40px; text-align: right; }
  .row-pct { color: var(--text); font-variant-numeric: tabular-nums; min-width: 56px; text-align: right; font-weight: 500; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; }
  .card-head { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 8px; }
  .card-head h3 { font-size: 14px; margin: 0; font-weight: 600; text-transform: capitalize; }
  .card-sub { font-size: 11px; text-transform: uppercase; letter-spacing: 0.6px; color: var(--muted);
    margin: 12px 0 6px; font-weight: 600; }
  .card .rows .row { grid-template-columns: 1fr 2fr auto; }
  ul.benches { list-style: none; margin: 0; padding: 0; }
  ul.benches li { display: flex; justify-content: space-between; padding: 3px 0; font-size: 12px;
    color: var(--muted); }
  ul.benches b { color: var(--text); font-variant-numeric: tabular-nums; font-weight: 500; }
  .muted { color: var(--muted); font-size: 12px; }
  footer { color: var(--muted); font-size: 12px; margin-top: 32px; display: flex; gap: 16px; flex-wrap: wrap; }
  footer a { color: var(--accent-2); text-decoration: none; }
  footer a:hover { text-decoration: underline; }
  @media (max-width: 720px) { .stats { grid-template-columns: repeat(2, 1fr); } }
</style>
</head>
<body>
<main>
  <header class="top">
    <h1>tryaii-dre routing eval <small>${summary.totalPrompts} prompts</small></h1>
    <span class="meta">${esc(generatedAt)}</span>
  </header>
  <div class="meta">input: <code>${esc(inputPath)}</code></div>

  <div class="chips">
    ${priorityChip('quality', quality)}
    ${priorityChip('cost', cost)}
    ${priorityChip('speed', speed)}
  </div>

  <div class="stats">
    <div class="stat"><div class="k">Successes</div><div class="v">${summary.successCount}</div></div>
    <div class="stat"><div class="k">Errors</div><div class="v${summary.errorCount > 0 ? ' warn' : ''}">${summary.errorCount}</div></div>
    <div class="stat"><div class="k">Distinct models</div><div class="v">${summary.distinctModels}</div></div>
    <div class="stat"><div class="k">Avg route</div><div class="v">${summary.avgRouteMs} <span class="muted" style="font-size:13px">ms</span></div></div>
  </div>

  <section>
    <h2>Recommended models — overall</h2>
    <div class="panel"><ul class="rows">${distRows}</ul></div>
  </section>

  <section>
    <h2>By category</h2>
    <div class="grid">${categoryCards}</div>
  </section>

  <footer>
    <span>artifacts:</span>
    <a href="${esc(summaryHref)}">summary.json</a>
    <a href="${esc(resultsHref)}">results.jsonl</a>
  </footer>
</main>
</body>
</html>
`;
}
