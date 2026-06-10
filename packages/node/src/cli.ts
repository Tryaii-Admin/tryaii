#!/usr/bin/env node
/**
 * TryAii-DRE CLI.
 *
 * Commands (kept in parity with the Python SDK's `tryaii`):
 *   tryaii route "your prompt here"   -- Route a prompt and show recommendations
 *   tryaii eval prompts.json          -- Route a JSON prompt dataset
 *   tryaii setup                      -- Download the embedding model + warm centroids
 *   tryaii models                     -- List available models
 *   tryaii benchmarks                 -- List available benchmarks
 *
 * Global flags: --no-banner, -v/--verbose, -V/--version, -h/--help.
 *
 * Exit codes (matched with the Python CLI): 0 success, 1 runtime failure,
 * 2 usage error (unknown command/option, missing argument, invalid value).
 */

import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { parseArgs } from 'node:util';

import { showBanner } from './banner.js';
import { BudgetMode, DifficultySource, routeDatasetWithBudget } from './budget.js';
import { benchmarkToDict, BenchmarkRegistry } from './benchmarks/registry.js';
import { CentroidGenerator } from './centroids/generator.js';
import { ClassificationResult } from './classifiers/base.js';
import { centroidFilePath, createDefaultConfig, DEFAULT_EMBEDDING_MODEL } from './config.js';
import { LocalEmbeddingProvider } from './embeddings/local.js';
import { DashboardSummary, renderDashboard } from './dashboard/index.js';
import { ModelInfo, ModelRegistry } from './registry/models.js';
import {
  Router,
  RouteResult,
  routeResultBestReasoning,
  routeResultBestScore,
} from './router.js';
import { Priorities } from './scoring/priorities.js';

/** Error type whose message is shown to the user without a stack trace. */
class CliError extends Error {}

/** Bad invocation (unknown command/option, missing argument, invalid value); exits 2 like argparse. */
class CliUsageError extends CliError {}

const out = process.stdout;

function intFlag(name: string, value: string | undefined, fallback: number): number {
  if (value === undefined) return fallback;
  const parsed = Number(value);
  if (!Number.isInteger(parsed)) {
    throw new CliUsageError(`${name} expects an integer, got '${value}'`);
  }
  return parsed;
}

function floatFlag(name: string, value: string): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    throw new CliUsageError(`${name} expects a number, got '${value}'`);
  }
  return parsed;
}

function round(value: number, digits: number): number {
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
}

// ---------------------------------------------------------------------------
// route
// ---------------------------------------------------------------------------

async function cmdRoute(subArgs: string[]): Promise<void> {
  const { values, positionals } = parseArgs({
    args: subArgs,
    allowPositionals: true,
    options: {
      quality: { type: 'string', default: '3' },
      cost: { type: 'string', default: '3' },
      speed: { type: 'string', default: '3' },
      'top-k': { type: 'string', default: '5' },
    },
  });

  const prompt = positionals[0];
  if (!prompt) {
    throw new CliUsageError('route requires a prompt, e.g. tryaii route "Write a quicksort"');
  }

  const priorities = new Priorities(
    intFlag('--quality', values.quality, 3),
    intFlag('--cost', values.cost, 3),
    intFlag('--speed', values.speed, 3),
  );
  const topK = intFlag('--top-k', values['top-k'], 5);

  const router = new Router();
  const result = await router.route(prompt, { priorities, topK });
  const classification = result.classification;

  out.write(`\nPrompt: ${prompt}\n`);
  out.write(
    `Category: ${classification?.broadCategory ?? ''} > ${classification?.subcategory ?? ''}\n`,
  );
  out.write(`Confidence: ${(classification?.confidence ?? 0).toFixed(3)}\n`);
  out.write(`Classifier: ${classification?.classifierUsed ?? ''}\n`);
  out.write(`\nTop ${result.scores.length} Recommendations:\n`);
  out.write('-'.repeat(70) + '\n');

  result.scores.forEach((score, index) => {
    const model = router.models.getModel(score.modelId);
    const provider = model ? model.provider : '?';
    out.write(`  ${index + 1}. ${score.modelId}\n`);
    out.write(`     Provider: ${provider} | Score: ${score.finalScore.toFixed(3)}\n`);
    out.write(
      `     Quality: ${score.qualityScore.toFixed(3)} | ` +
        `Cost: ${score.costScore.toFixed(3)} | Speed: ${score.speedScore.toFixed(3)}\n`,
    );
    if (model?.pricing) {
      out.write(
        `     Pricing: $${model.pricing.inputPer1k.toFixed(4)}/` +
          `$${model.pricing.outputPer1k.toFixed(4)} per 1k\n`,
      );
    }
    out.write(`     Reason: ${score.reasoning}\n\n`);
  });
}

// ---------------------------------------------------------------------------
// models
// ---------------------------------------------------------------------------

async function cmdModels(subArgs: string[]): Promise<void> {
  const { values } = parseArgs({
    args: subArgs,
    allowPositionals: true,
    options: {
      provider: { type: 'string' },
      json: { type: 'boolean', default: false },
    },
  });

  const registry = ModelRegistry.default();
  let models = registry.allModels;
  if (values.provider) {
    const provider = values.provider.toLowerCase();
    models = models.filter((m) => m.provider.toLowerCase() === provider);
  }

  if (values.json) {
    out.write(JSON.stringify(models.map((m) => m.toDict()), null, 2) + '\n');
    return;
  }

  out.write(`\nAvailable Models (${models.length}):\n`);
  out.write('-'.repeat(70) + '\n');

  const byProvider = new Map<string, ModelInfo[]>();
  for (const model of models) {
    const list = byProvider.get(model.provider) ?? [];
    list.push(model);
    byProvider.set(model.provider, list);
  }

  for (const provider of [...byProvider.keys()].sort()) {
    const providerModels = byProvider.get(provider) as ModelInfo[];
    out.write(`\n  ${provider} (${providerModels.length} models):\n`);
    for (const model of providerModels) {
      const latency = model.latency ?? '?';
      let price = '';
      if (model.pricing) {
        price = ` | $${model.pricing.inputPer1k.toFixed(4)}/${model.pricing.outputPer1k.toFixed(4)}`;
      }
      out.write(`    - ${model.modelId} [${latency}]${price}\n`);
    }
  }
}

// ---------------------------------------------------------------------------
// benchmarks
// ---------------------------------------------------------------------------

async function cmdBenchmarks(subArgs: string[]): Promise<void> {
  const { values } = parseArgs({
    args: subArgs,
    allowPositionals: true,
    options: { json: { type: 'boolean', default: false } },
  });

  const registry = BenchmarkRegistry.default();

  if (values.json) {
    out.write(JSON.stringify(registry.allBenchmarks.map(benchmarkToDict), null, 2) + '\n');
    return;
  }

  out.write(`\nAvailable Benchmarks (${registry.length}):\n`);
  out.write('-'.repeat(60) + '\n');
  for (const benchmark of registry.allBenchmarks) {
    const norm = `[${benchmark.normalization.minScore}-${benchmark.normalization.maxScore}]`;
    out.write(`  ${benchmark.name.padEnd(30)} ${norm.padEnd(15)} ${benchmark.description}\n`);
  }
}

// ---------------------------------------------------------------------------
// setup
// ---------------------------------------------------------------------------

async function cmdSetup(subArgs: string[]): Promise<void> {
  const { values } = parseArgs({
    args: subArgs,
    allowPositionals: true,
    options: { model: { type: 'string' } },
  });

  const embeddingModel = values.model ?? DEFAULT_EMBEDDING_MODEL;
  out.write(`Setting up TryAii-DRE with embedding model: ${embeddingModel}\n`);
  out.write('This will download the model and load benchmark centroids (one-time operation)...\n\n');

  const router = values.model
    ? new Router({ config: { embeddingModel: values.model } })
    : new Router();
  await router.route('warmup');

  out.write(`Setup complete! ${router.benchmarks.length} benchmark centroids ready.\n`);
}

// ---------------------------------------------------------------------------
// regenerate
// ---------------------------------------------------------------------------

async function cmdRegenerate(subArgs: string[]): Promise<void> {
  const { values } = parseArgs({
    args: subArgs,
    allowPositionals: true,
    options: { model: { type: 'string' } },
  });

  const embeddingModel = values.model ?? DEFAULT_EMBEDDING_MODEL;
  out.write(`Regenerating centroids for: ${embeddingModel}\n`);

  const config = createDefaultConfig(values.model ? { embeddingModel } : undefined);
  const provider = new LocalEmbeddingProvider(`Xenova/${embeddingModel}`);
  const generator = new CentroidGenerator(provider);
  const centroids = await generator.generateAsync();
  const path = centroidFilePath(config);
  generator.save(centroids, path);

  out.write(`Done! Generated ${Object.keys(centroids).length} centroids at ${path}\n`);
}

// ---------------------------------------------------------------------------
// eval
// ---------------------------------------------------------------------------

interface EvalRow {
  id: string;
  prompt: string;
  category: string;
}

function loadEvalPrompts(path: string): EvalRow[] {
  let raw = readFileSync(path, 'utf-8');
  if (raw.charCodeAt(0) === 0xfeff) raw = raw.slice(1); // strip BOM
  const data = JSON.parse(raw);
  if (!Array.isArray(data)) {
    throw new CliError(`Expected top-level JSON array in ${path}`);
  }

  return data.map((item: unknown, index: number): EvalRow => {
    const ordinal = index + 1;
    if (typeof item === 'string') {
      return { id: `p${ordinal}`, prompt: item, category: 'unknown' };
    }
    if (
      item != null &&
      typeof item === 'object' &&
      typeof (item as { prompt?: unknown }).prompt === 'string'
    ) {
      const obj = item as { id?: unknown; prompt: string; category?: unknown };
      return {
        id: String(obj.id ?? `p${ordinal}`),
        prompt: obj.prompt,
        category: String(obj.category ?? 'unknown'),
      };
    }
    throw new CliError(`Item at index ${index} is neither a string nor an object with prompt`);
  });
}

function topBenchmarksOf(
  classification: ClassificationResult | null,
  limit = 5,
): Array<{ name: string; score: number }> {
  if (!classification) return [];
  return Object.entries(classification.benchmarkScores)
    .sort((a, b) => b[1] - a[1])
    .slice(0, limit)
    .map(([name, score]) => ({ name, score: round(score, 4) }));
}

async function routeEvalRow(
  router: Router,
  row: EvalRow,
  priorities: Priorities,
  topK: number,
): Promise<Record<string, unknown>> {
  const started = Date.now();
  try {
    const result = await router.route(row.prompt, { priorities, topK });
    const classification = result.classification;
    return {
      id: row.id,
      category: row.category,
      prompt: row.prompt,
      bestModel: result.bestModel,
      bestScore: routeResultBestScore(result),
      bestReasoning: routeResultBestReasoning(result),
      topK: result.scores.map((s) => ({ modelId: s.modelId, finalScore: s.finalScore })),
      topBenchmarks: topBenchmarksOf(classification),
      broadCategory: classification?.broadCategory ?? '',
      subcategory: classification?.subcategory ?? '',
      confidence: classification?.confidence ?? 0,
      routeMs: round(Date.now() - started, 2),
    };
  } catch (error) {
    return {
      id: row.id,
      category: row.category,
      prompt: row.prompt,
      bestModel: '',
      bestScore: 0,
      bestReasoning: '',
      topK: [],
      topBenchmarks: [],
      broadCategory: '',
      subcategory: '',
      confidence: 0,
      routeMs: round(Date.now() - started, 2),
      error: error instanceof Error ? error.message : String(error),
    };
  }
}

function counts(values: string[]): Map<string, number> {
  const map = new Map<string, number>();
  for (const value of values) map.set(value, (map.get(value) ?? 0) + 1);
  return map;
}

/** count desc, ties by insertion order (matches Python Counter.most_common). */
function mostCommon(map: Map<string, number>): Array<[string, number]> {
  return [...map.entries()].sort((a, b) => b[1] - a[1]);
}

function buildEvalSummary(
  results: Record<string, unknown>[],
  priorities: Priorities,
): DashboardSummary {
  const successes = results.filter((row) => !row.error);
  const totalMs = results.reduce((sum, row) => sum + Number(row.routeMs), 0);
  const modelCounts = counts(successes.map((row) => String(row.bestModel)));

  const distribution = mostCommon(modelCounts).map(([model, count]) => ({
    model,
    count,
    pct: round((count / Math.max(1, successes.length)) * 100, 2),
  }));

  const byCategory = new Map<string, Record<string, unknown>[]>();
  for (const row of successes) {
    const category = String(row.category);
    const list = byCategory.get(category) ?? [];
    list.push(row);
    byCategory.set(category, list);
  }

  const categories = [];
  for (const [category, rows] of byCategory) {
    const categoryModels = counts(rows.map((row) => String(row.bestModel)));
    const benchTotals = new Map<string, number>();
    const benchCounts = new Map<string, number>();
    for (const row of rows) {
      const benches = (row.topBenchmarks ?? []) as Array<{ name?: string; score?: number }>;
      for (const bench of benches) {
        if (!bench.name) continue;
        benchTotals.set(bench.name, (benchTotals.get(bench.name) ?? 0) + Number(bench.score ?? 0));
        benchCounts.set(bench.name, (benchCounts.get(bench.name) ?? 0) + 1);
      }
    }
    const benchAvgs = [...benchTotals.keys()]
      .map((name) => ({
        name,
        avgScore: round((benchTotals.get(name) as number) / (benchCounts.get(name) as number), 4),
      }))
      .sort((a, b) => b.avgScore - a.avgScore);

    categories.push({
      category,
      count: rows.length,
      topModels: mostCommon(categoryModels).map(([model, count]) => ({
        model,
        count,
        pct: round((count / rows.length) * 100, 2),
      })),
      topBenchmarks: benchAvgs.slice(0, 5),
    });
  }

  return {
    totalPrompts: results.length,
    successCount: successes.length,
    errorCount: results.length - successes.length,
    distinctModels: modelCounts.size,
    avgRouteMs: round(totalMs / Math.max(1, results.length), 2),
    totalRouteMs: round(totalMs, 2),
    priorities: priorities.toDict(),
    distribution,
    byCategory: categories.sort((a, b) => b.count - a.count),
  };
}

function evalStampDir(): string {
  const now = new Date();
  const pad = (n: number): string => String(n).padStart(2, '0');
  return (
    `tryaii-eval-${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}` +
    `-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`
  );
}

async function cmdEval(subArgs: string[]): Promise<void> {
  const { values, positionals } = parseArgs({
    args: subArgs,
    allowPositionals: true,
    options: {
      output: { type: 'string', short: 'o' },
      quality: { type: 'string', default: '3' },
      cost: { type: 'string', default: '3' },
      speed: { type: 'string', default: '3' },
      'top-k': { type: 'string', default: '5' },
      'max-price': { type: 'string' },
      'output-tokens': { type: 'string', default: '1000' },
      'budget-mode': { type: 'string', default: 'strict' },
      'difficulty-source': { type: 'string', default: 'intrinsic' },
      'difficulty-gamma': { type: 'string', default: '1' },
    },
  });

  const inputArg = positionals[0];
  if (!inputArg) {
    throw new CliUsageError('eval requires an input JSON file, e.g. tryaii eval prompts.json');
  }
  const inputPath = resolve(inputArg);
  const outputDir = values.output ? resolve(values.output) : resolve(process.cwd(), evalStampDir());

  const priorities = new Priorities(
    intFlag('--quality', values.quality, 3),
    intFlag('--cost', values.cost, 3),
    intFlag('--speed', values.speed, 3),
  );
  const topK = intFlag('--top-k', values['top-k'], 5);
  const maxPrice =
    values['max-price'] != null ? floatFlag('--max-price', values['max-price']) : null;
  const outputTokens = intFlag('--output-tokens', values['output-tokens'], 1000);
  const budgetMode = (values['budget-mode'] ?? 'strict') as BudgetMode;
  if (budgetMode !== 'strict' && budgetMode !== 'fit-output') {
    throw new CliUsageError("--budget-mode must be 'strict' or 'fit-output'");
  }
  const difficultySource = (values['difficulty-source'] ?? 'intrinsic') as DifficultySource;
  if (
    difficultySource !== 'intrinsic' &&
    difficultySource !== 'capability' &&
    difficultySource !== 'blend'
  ) {
    throw new CliUsageError("--difficulty-source must be 'intrinsic', 'capability', or 'blend'");
  }
  const difficultyGamma = floatFlag('--difficulty-gamma', values['difficulty-gamma'] ?? '1');
  if (difficultyGamma < 0) {
    throw new CliUsageError('--difficulty-gamma must be a non-negative number');
  }

  const rows = loadEvalPrompts(inputPath);

  out.write(`[eval] input      : ${inputPath}\n`);
  out.write(`[eval] output     : ${outputDir}\n`);
  if (maxPrice == null) {
    out.write(
      `[eval] priorities : quality=${priorities.quality} ` +
        `cost=${priorities.cost} speed=${priorities.speed}\n`,
    );
  } else {
    out.write('[eval] objective  : maximize quality under total budget\n');
    out.write('[eval] priorities : ignored for budgeted runs\n');
  }
  out.write(`[eval] loaded ${rows.length} prompt(s)\n`);

  const router = new Router();
  out.write('[eval] warming up router...\n');
  await router.route('warmup', { priorities, topK: 1 });

  let results: Record<string, unknown>[];
  let budgetSummary: Record<string, unknown> | null = null;

  if (maxPrice != null) {
    out.write(
      `[eval] budget     : $${maxPrice.toFixed(6)} total, ` +
        `${outputTokens} output tokens/prompt, mode=${budgetMode}, difficulty=${difficultySource}\n`,
    );
    let nextPct = 10;
    const progress = (done: number, total: number): void => {
      const pct = Math.floor((done / Math.max(1, total)) * 100);
      if (pct >= nextPct || done === total) {
        out.write(`[eval] built candidates ${done}/${total} (${Math.min(pct, 100)}%)\n`);
        while (nextPct <= pct) nextPct += 10;
      }
    };

    const { results: budgeted, optimization } = await routeDatasetWithBudget({
      router,
      prompts: rows.map((row) => row.prompt),
      priorities,
      maxPrice,
      outputTokens,
      budgetMode,
      difficultySource,
      difficultyGamma,
      progressCallback: progress,
    });

    results = budgeted.map((budgetedResult) => {
      const selected = budgetedResult.selected;
      const row = rows[selected.promptIndex];
      const classification = budgetedResult.routeResult.classification;
      return {
        id: row.id,
        category: row.category,
        prompt: row.prompt,
        bestModel: selected.modelId,
        normalBestModel: selected.normalBestModel,
        budgetConstrained: selected.modelId !== selected.normalBestModel,
        bestScore: selected.finalScore,
        bestReasoning: selected.reasoning,
        difficulty: round(selected.difficulty, 4),
        estimatedCost: round(selected.estimatedCost, 8),
        cumulativeCost: round(budgetedResult.cumulativeCost, 8),
        remainingBudget: round(budgetedResult.remainingBudget, 8),
        inputTokens: selected.inputTokens,
        outputTokens: selected.outputTokens,
        topK: budgetedResult.routeResult.scores
          .slice(0, topK)
          .map((s) => ({ modelId: s.modelId, finalScore: s.finalScore })),
        topBenchmarks: topBenchmarksOf(classification),
        broadCategory: classification?.broadCategory ?? '',
        subcategory: classification?.subcategory ?? '',
        confidence: classification?.confidence ?? 0,
        routeMs: budgetedResult.routeMs,
        optimizerStatus: optimization.status,
      };
    });

    const minRequired = optimization.minimumRequiredBudget;
    const requestedMin = optimization.requestedMinimumRequiredBudget;
    const shortfall = optimization.budgetShortfall;
    budgetSummary = {
      status: optimization.status,
      budget: optimization.budget,
      budgetMode: optimization.budgetMode,
      difficultySource,
      selectionObjective: 'maximizeQualityUnderBudget',
      prioritiesIgnored: true,
      requestedOutputTokens: optimization.requestedOutputTokens,
      effectiveOutputTokens: optimization.effectiveOutputTokens,
      outputTokens: optimization.effectiveOutputTokens,
      totalEstimatedCost: round(optimization.totalEstimatedCost, 8),
      minimumRequiredBudget: Number.isFinite(minRequired) ? round(minRequired, 8) : null,
      requestedMinimumRequiredBudget:
        requestedMin != null && Number.isFinite(requestedMin) ? round(requestedMin, 8) : null,
      budgetShortfall: shortfall != null && Number.isFinite(shortfall) ? round(shortfall, 8) : null,
      costUnit: optimization.costUnit,
      message: optimization.message,
    };

    out.write(`[eval] optimizer status: ${optimization.status}\n`);
    if (
      optimization.requestedOutputTokens != null &&
      optimization.effectiveOutputTokens != null &&
      optimization.effectiveOutputTokens !== optimization.requestedOutputTokens
    ) {
      out.write(
        `[eval] output fit : ${optimization.requestedOutputTokens} -> ` +
          `${optimization.effectiveOutputTokens} tokens/prompt\n`,
      );
    }
  } else {
    results = [];
    let nextPct = 10;
    const total = rows.length;
    for (let index = 0; index < rows.length; index++) {
      results.push(await routeEvalRow(router, rows[index], priorities, topK));
      const pct = Math.floor(((index + 1) / Math.max(1, total)) * 100);
      if (pct >= nextPct || index + 1 === total) {
        out.write(`[eval] routed ${index + 1}/${total} (${Math.min(pct, 100)}%)\n`);
        while (nextPct <= pct) nextPct += 10;
      }
    }
  }

  mkdirSync(outputDir, { recursive: true });
  const resultsPath = join(outputDir, 'results.jsonl');
  const summaryPath = join(outputDir, 'summary.json');
  const dashboardPath = join(outputDir, 'index.html');

  writeFileSync(
    resultsPath,
    results.map((row) => JSON.stringify(row)).join('\n') + (results.length ? '\n' : ''),
    'utf-8',
  );

  const summary = buildEvalSummary(results, priorities) as DashboardSummary & {
    budget?: Record<string, unknown>;
  };
  if (budgetSummary) summary.budget = budgetSummary;
  writeFileSync(summaryPath, JSON.stringify(summary, null, 2), 'utf-8');
  writeFileSync(dashboardPath, renderDashboard(summary, inputPath), 'utf-8');

  out.write('\n[eval] === Summary ===\n');
  out.write(`Prompts        : ${summary.totalPrompts}\n`);
  out.write(`Successes      : ${summary.successCount}\n`);
  out.write(`Errors         : ${summary.errorCount}\n`);
  out.write(`Distinct models: ${summary.distinctModels}\n`);
  out.write(`Avg route time : ${summary.avgRouteMs} ms\n`);
  if (budgetSummary) {
    out.write(`Budget status  : ${budgetSummary.status}\n`);
    out.write(`Estimated cost : $${Number(budgetSummary.totalEstimatedCost).toFixed(6)}\n`);
    out.write(`Budget         : $${Number(budgetSummary.budget).toFixed(6)}\n`);
  }
  out.write('\nTop recommended models:\n');
  for (const row of summary.distribution.slice(0, 10)) {
    out.write(`  ${row.model.padEnd(40)} ${String(row.count).padStart(5)}  (${row.pct}%)\n`);
  }
  out.write(`\n[eval] per-prompt results -> ${resultsPath}\n`);
  out.write(`[eval] summary            -> ${summaryPath}\n`);
  out.write(`[eval] dashboard          -> ${dashboardPath}\n`);

  // Exit non-zero when every prompt errored so callers / CI can detect a total failure.
  if (summary.totalPrompts > 0 && summary.errorCount === summary.totalPrompts) {
    const firstError = results.find((row) => row.error)?.error ?? 'all prompts failed to route';
    process.stderr.write(
      `[eval] error: all ${summary.totalPrompts} prompt(s) failed: ${firstError}\n`,
    );
    process.exitCode = 1;
  }
}

// ---------------------------------------------------------------------------
// help / dispatch
// ---------------------------------------------------------------------------

const HELP = `tryaii -- Embedding-based AI model router

Usage:
  tryaii <command> [options]

Commands:
  route <prompt>        Route a prompt to the best model and show recommendations
  eval <input.json>     Route a JSON dataset; writes results.jsonl, summary.json, index.html
  models                List available models (--provider <name>, --json)
  benchmarks            List available benchmarks (--json)
  setup                 Download the embedding model and warm centroids (--model <name>)
  regenerate            Rebuild benchmark centroids, e.g. after changing the embedding model (--model <name>)

Common options:
  --quality <1-5>       Quality priority for route/eval (default 3)
  --cost <1-5>          Cost priority for route/eval (default 3)
  --speed <1-5>         Speed priority for route/eval (default 3)
  --top-k <n>           Number of recommendations (default 5)

Eval-only options:
  -o, --output <dir>    Output directory (default: ./tryaii-eval-<timestamp>)
  --max-price <usd>     Total dataset budget; switches eval to budget-optimized mode
  --output-tokens <n>   Expected output tokens per prompt for budget estimation (default 1000)
  --budget-mode <mode>  'strict' (default) or 'fit-output'
  --difficulty-source <s>  Gauge task complexity: 'intrinsic' (default), 'capability', or 'blend'
  --difficulty-gamma <n>   How hard to shift budget toward complex prompts (default 1; 0 disables)

Global flags:
  --no-banner           Disable the startup banner (also honored via TRYAII_NO_BANNER)
  -v, --verbose         Enable verbose logging
  -V, --version         Print the version and exit
  -h, --help            Show this help

Examples:
  tryaii route "Write a Python function to merge sorted arrays" --quality=5 --cost=1
  tryaii eval prompts.json --output results/run --quality=5 --cost=1 --speed=1
  tryaii eval prompts.json --max-price=0.10 --output-tokens=2000 --budget-mode=fit-output
  tryaii eval prompts.json --max-price=0.50 --difficulty-source=intrinsic --difficulty-gamma=2
`;

function version(): string {
  try {
    // dist/cli.js -> ../package.json resolves to the package root.
    return (
      (JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf-8')) as {
        version?: string;
      }).version ?? '0.0.0'
    );
  } catch {
    return '0.0.0';
  }
}

async function main(): Promise<void> {
  const argv = process.argv.slice(2);

  if (argv.includes('--version') || argv.includes('-V')) {
    out.write(version() + '\n');
    return;
  }

  const noBanner = argv.includes('--no-banner') || Boolean(process.env.TRYAII_NO_BANNER);
  // Accepted anywhere for cross-SDK compatibility; the Python CLI uses it to
  // enable debug logging. The Node SDK has no logging today, so this only
  // exposes the intent to downstream code via the environment.
  const verbose = argv.includes('--verbose') || argv.includes('-v');
  if (verbose) process.env.TRYAII_VERBOSE = '1';
  const filtered = argv.filter(
    (arg) => arg !== '--no-banner' && arg !== '--verbose' && arg !== '-v',
  );
  const command = filtered[0];
  const subArgs = filtered.slice(1);

  if (!noBanner) await showBanner();

  // Like --no-banner/--verbose, help is honored anywhere, including after a
  // subcommand (e.g. `tryaii eval --help`).
  if (
    !command ||
    command === 'help' ||
    filtered.includes('-h') ||
    filtered.includes('--help')
  ) {
    out.write(HELP);
    return;
  }

  switch (command) {
    case 'route':
      await cmdRoute(subArgs);
      break;
    case 'eval':
      await cmdEval(subArgs);
      break;
    case 'models':
      await cmdModels(subArgs);
      break;
    case 'benchmarks':
      await cmdBenchmarks(subArgs);
      break;
    case 'setup':
      await cmdSetup(subArgs);
      break;
    case 'regenerate':
      await cmdRegenerate(subArgs);
      break;
    default:
      throw new CliUsageError(`Unknown command: ${command}\nRun "tryaii --help" for usage.`);
  }
}

/** parseArgs error codes that indicate a bad invocation rather than a runtime failure. */
function isParseArgsUsageError(error: unknown): boolean {
  const code = (error as { code?: string }).code ?? '';
  return code.startsWith('ERR_PARSE_ARGS_');
}

main().catch((error) => {
  const message =
    error instanceof CliError
      ? error.message
      : error instanceof Error
        ? error.message
        : String(error);
  process.stderr.write(`error: ${message}\n`);
  process.exitCode = error instanceof CliUsageError || isParseArgsUsageError(error) ? 2 : 1;
});
