import { describe, expect, it } from 'vitest';

import { renderDashboard, type DashboardSummary } from '../src/index.js';

const fixture: DashboardSummary = {
  totalPrompts: 12,
  successCount: 11,
  errorCount: 1,
  distinctModels: 3,
  avgRouteMs: 42,
  totalRouteMs: 504,
  priorities: { quality: 5, cost: 3, speed: 1 },
  distribution: [
    { model: 'anthropic/claude-opus-4', count: 7, pct: 58 },
    { model: 'openai/gpt-4o', count: 3, pct: 25 },
    { model: 'meta/llama-3-70b', count: 2, pct: 17 },
  ],
  byCategory: [
    {
      category: 'math',
      count: 5,
      topModels: [
        { model: 'anthropic/claude-opus-4', count: 4, pct: 80 },
        { model: 'openai/gpt-4o', count: 1, pct: 20 },
      ],
      topBenchmarks: [
        { name: 'gsm8k', avgScore: 0.912 },
        { name: 'math', avgScore: 0.834 },
      ],
    },
  ],
};

describe('renderDashboard', () => {
  it('renders a self-contained HTML document with the run summary', () => {
    const html = renderDashboard(fixture, '/runs/quality');

    expect(html.startsWith('<!doctype html>')).toBe(true);
    expect(html).toContain('<title>tryaii-dre eval — 12 prompts</title>');
    expect(html).toContain('anthropic/claude-opus-4');
    expect(html).toContain('gsm8k');
    expect(html).toContain('/runs/quality');
    // Default footer links resolve relative to the run dir.
    expect(html).toContain('href="summary.json"');
    expect(html).toContain('href="results.jsonl"');
  });

  it('honors override hrefs for the artifact footer links', () => {
    const html = renderDashboard(fixture, '/runs/quality', {
      summaryHref: 'file:///tmp/summary.json',
      resultsHref: 'file:///tmp/results.jsonl',
    });

    expect(html).toContain('href="file:///tmp/summary.json"');
    expect(html).toContain('href="file:///tmp/results.jsonl"');
    expect(html).not.toContain('href="summary.json"');
  });

  it('escapes user-controlled strings to prevent HTML injection', () => {
    const malicious: DashboardSummary = {
      ...fixture,
      byCategory: [
        {
          category: '<script>alert(1)</script>',
          count: 1,
          topModels: [{ model: 'evil"<>&\'model', count: 1, pct: 100 }],
          topBenchmarks: [{ name: '<img src=x>', avgScore: 0.5 }],
        },
      ],
    };

    const html = renderDashboard(malicious, '<script>bad</script>');

    expect(html).not.toContain('<script>alert(1)</script>');
    expect(html).not.toContain('<img src=x>');
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;');
    expect(html).toContain('&lt;img src=x&gt;');
    expect(html).toContain('evil&quot;&lt;&gt;&amp;&#39;model');
  });
});
