#!/usr/bin/env node
/**
 * TryAii CLI.
 *
 * Commands (kept in parity with the Python SDK's `tryaii`):
 *   tryaii route "your prompt here"   -- Route a prompt and show recommendations
 *   tryaii eval prompts.json          -- Route a JSON prompt dataset
 *   tryaii cachelint input.json       -- Pre-flight prompt-cache analysis
 *   tryaii setup                      -- Download the embedding model + warm centroids
 *   tryaii models                     -- List available models
 *   tryaii benchmarks                 -- List available benchmarks
 *   tryaii help [command]             -- Global help, or detailed help for one command
 *
 * Per-command help is also reachable via `tryaii <command> -h/--help`.
 * Global flags: --no-banner, -v/--verbose, -V/--version, -h/--help.
 *
 * Exit codes (matched with the Python CLI): 0 success, 1 runtime failure,
 * 2 usage error (unknown command/option, missing argument, invalid value).
 */

import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { parseArgs } from 'node:util';

import { showBanner } from './banner.js';
import { BudgetMode, DifficultySource, routeDatasetWithBudget } from './budget.js';
// Half-even formatting matches Python's f"{x:.2f}" (parity for money lines
// in the diagnose summary); halfEven.ts is tiny and data-free.
import { formatFixed } from './cachelint/util/halfEven.js';
import { benchmarkToDict, BenchmarkRegistry } from './benchmarks/registry.js';
import { CentroidGenerator } from './centroids/generator.js';
import { ClassificationResult } from './classifiers/base.js';
import {
  centroidFilePath,
  createDefaultConfig,
  DEFAULT_EMBEDDING_MODEL,
  TryaiiDreConfig,
} from './config.js';
import * as daemon from './daemon.js';
import { LocalEmbeddingProvider } from './embeddings/local.js';
import { DashboardSummary, renderDashboard } from './dashboard/index.js';
import { ModelInfo, ModelRegistry } from './registry/models.js';
import { writePaced } from './output.js';
import {
  MAX_PROMPT_LENGTH,
  Router,
  RouteResult,
  routeResultBestReasoning,
  routeResultBestScore,
} from './router.js';
import { Priorities } from './scoring/priorities.js';

/** A routing call backed by either a warm daemon or an in-process Router. */
type RouteFn = (prompt: string, priorities: Priorities, topK: number) => Promise<RouteResult>;

/**
 * Resolve a routing function, preferring a warm background daemon (auto-starting
 * one if needed) so repeated CLI calls skip the embedding-model load. Falls back
 * to an in-process Router when the daemon is disabled, unavailable, or slow to
 * start.
 */
async function acquireRouteFn(
  config: TryaiiDreConfig,
  noDaemon: boolean,
): Promise<{ routeFn: RouteFn; source: 'daemon' | 'inprocess' }> {
  if (!noDaemon && !daemon.isDisabled()) {
    let state: daemon.DaemonState | null = null;
    try {
      state = await daemon.ensureDaemon(config, {
        onStarting: () =>
          process.stderr.write(
            '[tryaii] starting routing daemon (first run loads the embedding ' +
              'model, this can take a minute)...\n',
          ),
      });
    } catch {
      state = null;
    }
    if (state) {
      const ready = state;
      return {
        routeFn: (prompt, priorities, topK) =>
          daemon.routeViaDaemon(ready, prompt, priorities, topK),
        source: 'daemon',
      };
    }
  }

  const router = new Router({ config: { embeddingModel: config.embeddingModel } });
  return {
    routeFn: (prompt, priorities, topK) => router.route(prompt, { priorities, topK }),
    source: 'inprocess',
  };
}

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
      'no-daemon': { type: 'boolean', default: false },
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

  const config = createDefaultConfig();
  const { routeFn } = await acquireRouteFn(config, Boolean(values['no-daemon']));
  const result = await routeFn(prompt, priorities, topK);
  const classification = result.classification;

  // Provider/pricing for display come from the (cheap) model registry so the
  // daemon path doesn't need to ship them over the wire.
  const registry = ModelRegistry.default();

  let buf = `\nPrompt: ${prompt}\n`;
  buf += `Category: ${classification?.broadCategory ?? ''} > ${classification?.subcategory ?? ''}\n`;
  buf += `Confidence: ${(classification?.confidence ?? 0).toFixed(3)}\n`;
  buf += `Classifier: ${classification?.classifierUsed ?? ''}\n`;
  buf += `\nTop ${result.scores.length} Recommendations:\n`;
  buf += '-'.repeat(70) + '\n';

  result.scores.forEach((score, index) => {
    const model = registry.getModel(score.modelId);
    const provider = model ? model.provider : '?';
    buf += `  ${index + 1}. ${score.modelId}\n`;
    buf += `     Provider: ${provider} | Score: ${score.finalScore.toFixed(3)}\n`;
    buf +=
      `     Quality: ${score.qualityScore.toFixed(3)} | ` +
      `Cost: ${score.costScore.toFixed(3)} | Speed: ${score.speedScore.toFixed(3)}\n`;
    if (model?.pricing) {
      buf +=
        `     Pricing: $${model.pricing.inputPer1k.toFixed(4)}/` +
        `$${model.pricing.outputPer1k.toFixed(4)} per 1k\n`;
    }
    buf += `     Reason: ${score.reasoning}\n\n`;
  });

  await writePaced(buf);
}

// ---------------------------------------------------------------------------
// models
// ---------------------------------------------------------------------------

async function cmdCachelint(subArgs: string[]): Promise<void> {
  const { values, positionals } = parseArgs({
    args: subArgs,
    allowPositionals: true,
    options: {
      provider: { type: 'string' },
      model: { type: 'string' },
      json: { type: 'boolean', default: false },
    },
  });

  if (values.model && !values.provider) {
    throw new CliUsageError('--model requires --provider (raw-text mode)');
  }
  const input = positionals[0];
  if (input === undefined) {
    throw new CliUsageError('cachelint: missing required argument: input');
  }

  let raw: string;
  if (input === '-') {
    // Mirror Python sys.stdin.read(): universal newlines, no BOM strip.
    raw = readFileSync(0, 'utf-8').replace(/\r\n/g, '\n');
  } else {
    let bytes: string;
    try {
      bytes = readFileSync(input, 'utf-8');
    } catch {
      throw new CliError(`file not found: ${input}`);
    }
    // Mirror Python's utf-8-sig + text-mode read: strip a BOM, normalize \r\n.
    if (bytes.charCodeAt(0) === 0xfeff) bytes = bytes.slice(1);
    raw = bytes.replace(/\r\n/g, '\n');
  }

  // Lazy import: the tokenizer rank data is multi-MB and must not load for
  // any other command (the cachelint module is also not in the root barrel).
  const cachelint = await import('./cachelint/index.js');

  let data: unknown;
  if (values.provider) {
    // Raw-text mode: the ENTIRE input is one prompt string, never parsed as JSON.
    data = { prompt: raw, llm: { provider: values.provider, name: values.model ?? '' } };
  } else {
    try {
      data = JSON.parse(raw);
    } catch {
      // Parser-neutral message (SPEC.md delta n): json and JSON.parse differ.
      const where = input === '-' ? 'on stdin' : `in '${input}'`;
      throw new CliUsageError(`invalid JSON ${where}`);
    }
  }

  let result: Record<string, unknown>;
  try {
    result = cachelint.analyze(data);
  } catch (error) {
    // Engine validation errors are usage errors (exit 2), like Python's ValueError path.
    throw new CliUsageError((error as Error).message);
  }

  if (values.json) {
    out.write(JSON.stringify(result, null, 2) + '\n');
  } else {
    await writePaced(cachelint.renderReport(result) + '\n');
  }
}

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

  let buf = `\nAvailable Models (${models.length}):\n`;
  buf += '-'.repeat(70) + '\n';

  const byProvider = new Map<string, ModelInfo[]>();
  for (const model of models) {
    const list = byProvider.get(model.provider) ?? [];
    list.push(model);
    byProvider.set(model.provider, list);
  }

  for (const provider of [...byProvider.keys()].sort()) {
    const providerModels = byProvider.get(provider) as ModelInfo[];
    buf += `\n  ${provider} (${providerModels.length} models):\n`;
    for (const model of providerModels) {
      const latency = model.latency ?? '?';
      let price = '';
      if (model.pricing) {
        price = ` | $${model.pricing.inputPer1k.toFixed(4)}/${model.pricing.outputPer1k.toFixed(4)}`;
      }
      buf += `    - ${model.modelId} [${latency}]${price}\n`;
    }
  }

  await writePaced(buf);
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

  let buf = `\nAvailable Benchmarks (${registry.length}):\n`;
  buf += '-'.repeat(60) + '\n';
  for (const benchmark of registry.allBenchmarks) {
    const norm = `[${benchmark.normalization.minScore}-${benchmark.normalization.maxScore}]`;
    buf += `  ${benchmark.name.padEnd(30)} ${norm.padEnd(15)} ${benchmark.description}\n`;
  }

  await writePaced(buf);
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
  out.write(`Setting up TryAii with embedding model: ${embeddingModel}\n`);
  out.write('This will download the model and load benchmark centroids (one-time operation)...\n\n');

  const router = values.model
    ? new Router({ config: { embeddingModel: values.model } })
    : new Router();
  await router.route('warmup');

  // Marker consumed by `diagnose check` (its setup gate): live classification
  // is only allowed once setup has completed at least once on this machine.
  const config = createDefaultConfig();
  mkdirSync(config.dataDir, { recursive: true });
  writeFileSync(
    join(config.dataDir, 'setup.json'),
    JSON.stringify(
      {
        embedding_model: embeddingModel,
        centroid_count: router.benchmarks.length,
        completed_at: new Date().toISOString().replace(/\.\d{3}Z$/, 'Z'),
      },
      null,
      2,
    ) + '\n',
    'utf-8',
  );

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
  routeFn: RouteFn,
  row: EvalRow,
  priorities: Priorities,
  topK: number,
): Promise<Record<string, unknown>> {
  const started = Date.now();
  try {
    const result = await routeFn(row.prompt, priorities, topK);
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
      'no-daemon': { type: 'boolean', default: false },
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

  const config = createDefaultConfig();
  let results: Record<string, unknown>[];
  let budgetSummary: Record<string, unknown> | null = null;

  if (maxPrice != null) {
    // Budget optimization drives the scoring engine directly, so it needs a
    // real in-process Router rather than the daemon's route() surface.
    const router = new Router({ config: { embeddingModel: config.embeddingModel } });
    out.write('[eval] warming up router...\n');
    await router.route('warmup', { priorities, topK: 1 });
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
    const { routeFn } = await acquireRouteFn(config, Boolean(values['no-daemon']));
    out.write('[eval] warming up router...\n');
    await routeFn('warmup', priorities, 1);
    results = [];
    let nextPct = 10;
    const total = rows.length;
    for (let index = 0; index < rows.length; index++) {
      results.push(await routeEvalRow(routeFn, rows[index], priorities, topK));
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

  let buf = '\n[eval] === Summary ===\n';
  buf += `Prompts        : ${summary.totalPrompts}\n`;
  buf += `Successes      : ${summary.successCount}\n`;
  buf += `Errors         : ${summary.errorCount}\n`;
  buf += `Distinct models: ${summary.distinctModels}\n`;
  buf += `Avg route time : ${summary.avgRouteMs} ms\n`;
  if (budgetSummary) {
    buf += `Budget status  : ${budgetSummary.status}\n`;
    buf += `Estimated cost : $${Number(budgetSummary.totalEstimatedCost).toFixed(6)}\n`;
    buf += `Budget         : $${Number(budgetSummary.budget).toFixed(6)}\n`;
  }
  buf += '\nTop recommended models:\n';
  for (const row of summary.distribution.slice(0, 10)) {
    buf += `  ${row.model.padEnd(40)} ${String(row.count).padStart(5)}  (${row.pct}%)\n`;
  }
  buf += `\n[eval] per-prompt results -> ${resultsPath}\n`;
  buf += `[eval] summary            -> ${summaryPath}\n`;
  buf += `[eval] dashboard          -> ${dashboardPath}\n`;
  await writePaced(buf);

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
// diagnose (see shared/diagnose/SPEC.md; mirrors cmd_diagnose in main.py)
// ---------------------------------------------------------------------------

async function diagnosePlan(argv: string[]): Promise<void> {
  const { values } = parseArgs({
    args: argv,
    allowPositionals: true,
    options: { json: { type: 'boolean', default: false } },
  });

  const raw = readFileSync(new URL('./diagnose/data/plan.json', import.meta.url), 'utf-8');
  if (values.json) {
    // Verbatim bytes of the bundled plan -- parity by construction.
    out.write(raw);
    return;
  }

  const plan = JSON.parse(raw) as Record<string, any>;
  let buf = 'tryaii diagnose plan -- what diagnose can check\n\n';
  buf += 'Checks (recommended: run all):\n';
  for (const check of plan.checks) {
    buf += `  - ${check.id}: ${check.what}\n`;
  }
  buf += '\nInterview -- ask the user:\n';
  for (const question of plan.interview) {
    buf += `  - ${question.ask}\n`;
  }
  const inv = plan.inventory;
  buf +=
    '\nInventory (per site) -- required: ' +
    inv.required_per_site.join(', ') +
    '; optional: ' +
    inv.optional_per_site.join(', ') +
    '\n';
  buf += 'Machine-readable plan with the full schema and an example: tryaii diagnose plan --json\n';
  buf += '\nNext:\n';
  buf += `  ${plan.commands.check}\n`;
  buf += `  ${plan.commands.report}\n`;
  await writePaced(buf);
}

/** Read + parse the inventory argument ('-' = stdin). Mirrors cachelint. */
function diagnoseReadInventory(inputArg: string): unknown {
  let raw: string;
  if (inputArg === '-') {
    raw = readFileSync(0, 'utf-8').replace(/\r\n/g, '\n');
  } else {
    let bytes: string;
    try {
      bytes = readFileSync(inputArg, 'utf-8');
    } catch {
      throw new CliError(`file not found: ${inputArg}`);
    }
    if (bytes.charCodeAt(0) === 0xfeff) bytes = bytes.slice(1);
    raw = bytes.replace(/\r\n/g, '\n');
  }
  try {
    return JSON.parse(raw);
  } catch {
    // Parser-neutral message: json and JSON.parse phrase errors differently.
    const where = inputArg === '-' ? 'on stdin' : `in '${inputArg}'`;
    throw new CliUsageError(`invalid JSON ${where}`);
  }
}

function diagnoseMoney(value: number): string {
  return '$' + formatFixed(value, 2);
}

/** Human summary of a check run -- byte-identical across both CLIs. */
function diagnoseSummaryText(findings: Record<string, any>, outDirDisplay: string): string {
  const summary = findings.summary;
  const skipped = findings.inventory.skipped.length;
  const skippedNote = skipped ? `, ${skipped} skipped` : '';
  let buf = `diagnose: ${summary.site_count} site(s) analyzed${skippedNote}\n\n`;
  for (const [check, counts] of Object.entries(summary.check_status_counts) as Array<
    [string, Record<string, number>]
  >) {
    buf +=
      `  ${check.padEnd(16)} ${counts.ok} ok | ${counts.finding} finding | ` +
      `${counts.insufficient_data} insufficient | ` +
      `${counts.skipped} skipped\n`;
  }
  const totals = summary.totals;
  if (totals.sites_with_traffic_data > 0) {
    buf += `\nmonthly estimates (${totals.sites_with_traffic_data} site(s) with traffic data):\n`;
    if (totals.est_monthly_cost_usd !== null) {
      buf += `  est. cost           ${diagnoseMoney(totals.est_monthly_cost_usd)}\n`;
    }
    if (totals.est_monthly_cache_savings_usd !== null) {
      buf += `  cache savings (max) ${diagnoseMoney(totals.est_monthly_cache_savings_usd)}\n`;
    }
    if (totals.est_monthly_swap_savings_usd !== null) {
      buf += `  swap savings        ${diagnoseMoney(totals.est_monthly_swap_savings_usd)}\n`;
    }
  } else {
    buf += '\nmonthly estimates: no traffic data (pass --calls-per-day or per-site calls_per_day)\n';
  }
  const runId = findings.run_id;
  buf += '\n';
  for (const name of ['inventory.json', 'findings.json', 'meta.json']) {
    buf += `-> ${outDirDisplay}/${runId}/${name}\n`;
  }
  return buf;
}

function diagnoseUtcStamp(): string {
  const d = new Date();
  const p = (n: number): string => String(n).padStart(2, '0');
  return (
    `${d.getUTCFullYear()}${p(d.getUTCMonth() + 1)}${p(d.getUTCDate())}` +
    `T${p(d.getUTCHours())}${p(d.getUTCMinutes())}${p(d.getUTCSeconds())}Z`
  );
}

async function diagnoseCheck(argv: string[]): Promise<void> {
  const { values, positionals } = parseArgs({
    args: argv,
    allowPositionals: true,
    options: {
      'quality': { type: 'string' },
      'cost': { type: 'string' },
      'speed': { type: 'string' },
      'checks': { type: 'string' },
      'calls-per-day': { type: 'string' },
      'output-tokens': { type: 'string' },
      'goal': { type: 'string' },
      'out-dir': { type: 'string', default: '.tryaii/diagnose' },
      'run-id': { type: 'string' },
      'now': { type: 'string' },
      'json': { type: 'boolean', default: false },
      'no-daemon': { type: 'boolean', default: false },
    },
  });

  const input = positionals[0];
  if (input === undefined) {
    throw new CliUsageError('diagnose check: missing required argument: inventory');
  }
  const quality = intFlag('--quality', values.quality, 3);
  const cost = intFlag('--cost', values.cost, 3);
  const speed = intFlag('--speed', values.speed, 3);

  // Lazy import: diagnose transitively loads the multi-MB tokenizer data.
  const diagnose = await import('./diagnose/index.js');

  let checks: string[] | null = null;
  if (values.checks !== undefined) {
    checks = values.checks
      .split(',')
      .map((c) => c.trim())
      .filter((c) => c.length > 0);
    const bad = checks.filter((c) => !diagnose.DEFAULT_CHECKS.includes(c));
    if (bad.length) {
      throw new CliUsageError(
        `unknown check '${bad[0]}'. Valid checks: ` + diagnose.DEFAULT_CHECKS.join(', '),
      );
    }
    if (!checks.length) {
      throw new CliUsageError(
        '--checks selected nothing. Valid checks: ' + diagnose.DEFAULT_CHECKS.join(', '),
      );
    }
  }

  const data = diagnoseReadInventory(input);

  // Live classification is needed only when model_fit is selected and at
  // least one prompt-bearing site lacks the _classification seam.
  const norm = diagnose.normalizeInventory(data);
  const needsRouting =
    (checks === null || checks.includes('model_fit')) &&
    norm.sites.some((site) => site.prompt !== null && site.classification === null);

  let classifyFn: ((canonical: string) => Promise<Record<string, unknown> | null>) | undefined;
  if (needsRouting) {
    const config = createDefaultConfig();
    if (!existsSync(join(config.dataDir, 'setup.json'))) {
      throw new CliError(
        "diagnose requires setup: run 'tryaii setup' first " +
          '(downloads the embedding model and warms centroids)',
      );
    }

    const prioritiesObj = new Priorities(quality, cost, speed);
    const { routeFn } = await acquireRouteFn(config, values['no-daemon']);

    classifyFn = async (canonical: string) => {
      const result = await routeFn(canonical.slice(0, MAX_PROMPT_LENGTH), prioritiesObj, 1);
      const c = result.classification;
      if (!c) return null;
      return {
        benchmark_similarities: { ...c.benchmarkScores },
        broad_category: c.broadCategory,
        subcategory: c.subcategory,
        confidence: c.confidence,
      };
    };
  }

  const runId = values['run-id'] ?? diagnoseUtcStamp();
  const now = values.now ?? new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');

  const findings = await diagnose.analyzeInventory(
    data,
    {
      run_id: runId,
      now,
      version: version(),
      priorities: { quality, cost, speed },
      goal: values.goal ?? null,
      checks,
      calls_per_day: values['calls-per-day'] !== undefined
        ? floatFlag('--calls-per-day', values['calls-per-day'])
        : null,
      output_tokens: values['output-tokens'] !== undefined
        ? intFlag('--output-tokens', values['output-tokens'], 500)
        : null,
    },
    classifyFn,
  );

  diagnose.writeRun(values['out-dir'], data, findings);

  if (values.json) {
    out.write(JSON.stringify(findings, null, 2) + '\n');
  } else {
    const outDirDisplay = values['out-dir'].replace(/\\/g, '/');
    await writePaced(diagnoseSummaryText(findings, outDirDisplay));
  }
}

/** Verb dispatcher for `tryaii diagnose` (verb-peeling, like `help <topic>`). */
async function cmdDiagnose(subArgs: string[]): Promise<void> {
  const verbs: Record<string, (argv: string[]) => Promise<void>> = {
    plan: diagnosePlan,
    check: diagnoseCheck,
  };
  const verb = subArgs[0];
  if (verb === undefined) {
    throw new CliUsageError('missing diagnose verb. Run "tryaii help diagnose".');
  }
  const handler = verbs[verb];
  if (handler === undefined) {
    throw new CliUsageError(`unknown diagnose verb: ${verb}. Run "tryaii help diagnose".`);
  }
  await handler(subArgs.slice(1));
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
  cachelint <input.json>  Analyze prompt-cache readiness before sending (--json, --provider)
  diagnose <verb>       Agent-driven codebase diagnostics: plan, check (see 'tryaii help diagnose')
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
  tryaii eval examples/prompts.json --output results/run --quality=5 --cost=1 --speed=1
  tryaii eval examples/prompts.json --max-price=0.10 --output-tokens=2000 --budget-mode=fit-output
  tryaii eval examples/prompts.json --max-price=0.50 --difficulty-source=intrinsic --difficulty-gamma=2
  tryaii cachelint request.json --json
`;

// Per-command help. Each constant must stay byte-identical to the matching
// triple-quoted string in packages/python/tryaii/cli/main.py (guarded by
// tests/test_parity.py). Keep the text backtick-free and ${-free so the parity
// test can compare the raw source between the backticks to the Python value.
const HELP_ROUTE = `tryaii route -- Route one prompt to the best model

Usage:
  tryaii route <prompt> [options]

Classify a prompt with local embeddings and print the top-K model
recommendations. Runs locally -- no API key needed, nothing is called.

Arguments:
  <prompt>              The prompt to route (required)

Options:
  --quality <1-5>       Quality priority (default 3; out-of-range clamped)
  --cost <1-5>          Cost priority (default 3; out-of-range clamped)
  --speed <1-5>         Speed priority (default 3; out-of-range clamped)
  --top-k <n>           Number of recommendations shown (default 5)
  --no-daemon           Route in-process for this call; do not use or start a daemon

Notes:
  Text output only -- there is no --json for route (use 'eval' or the SDK
  for machine-readable output). Scores are relative per call; do not
  compare them across prompts.
  A background daemon keeps the embedding model warm, so only the first
  call pays the multi-second model load. TRYAII_NO_DAEMON=1 disables it
  globally; TRYAII_DAEMON_IDLE=<s> tunes its idle shutdown (default 900).

Examples:
  tryaii route "Write a Python function to merge sorted arrays"
  tryaii route "Summarize this contract" --quality=5 --cost=1

Exit codes:
  0 success, 1 routing/embedding failure, 2 missing prompt or bad flag.

Docs: docs/cli/route.md
`;

const HELP_EVAL = `tryaii eval -- Route a JSON prompt dataset

Usage:
  tryaii eval <input.json> [options]

Route every prompt in a JSON file and write three artifacts into the output
directory: results.jsonl (per-prompt), summary.json (aggregate), and a
self-contained index.html dashboard. Runs locally -- no model APIs called.

Two modes:
  priority (default)    Route each prompt independently using your
                        quality/cost/speed weights.
  budget (--max-price)  Jointly maximize quality across the dataset under a
                        total USD budget. Priority weights are ignored.

Arguments:
  <input.json>          JSON array of prompt strings or {id,prompt,category}

Options:
  -o, --output <dir>    Output directory (default ./tryaii-eval-<timestamp>)
  --quality <1-5>       Quality priority (default 3; priority mode only)
  --cost <1-5>          Cost priority (default 3; priority mode only)
  --speed <1-5>         Speed priority (default 3; priority mode only)
  --top-k <n>           Models recorded per row (default 5)
  --max-price <usd>     Total dataset budget; switches eval to budget mode
  --output-tokens <n>   Assumed output tokens per prompt for costing (default 1000)
  --budget-mode <mode>  'strict' (default) or 'fit-output'
  --difficulty-source <s>  'intrinsic' (default), 'capability', or 'blend'
  --difficulty-gamma <n>   Shift budget toward harder prompts (default 1; 0 disables)
  --no-daemon           Route in-process for this call; do not use or start a daemon

Notes:
  A background daemon keeps the embedding model warm, so only the first
  call pays the multi-second model load. TRYAII_NO_DAEMON=1 disables it
  globally; TRYAII_DAEMON_IDLE=<s> tunes its idle shutdown (default 900).

Examples:
  tryaii eval examples/prompts.json --output results/run --quality=5 --cost=1 --speed=1
  tryaii eval examples/prompts.json --max-price=0.10 --output-tokens=2000 --budget-mode=fit-output
  tryaii eval examples/prompts.json --max-price=0.50 --difficulty-source=intrinsic --difficulty-gamma=2

Exit codes:
  0 success (incl. partial per-prompt failures), 1 bad input / warmup / all
  prompts failed, 2 usage error.

Docs: docs/cli/eval/README.md
`;

const HELP_MODELS = `tryaii models -- List the model catalog

Usage:
  tryaii models [options]

Print every model in the default registry, grouped by provider. Each line
shows: model_id [latency-tier] | $input/output per 1k tokens (price omitted
when the model has no pricing).

Options:
  --provider <name>     Filter to one provider (case-insensitive exact match)
  --json                Print the (filtered) models as pretty-printed JSON

Examples:
  tryaii models
  tryaii models --provider anthropic
  tryaii models --json

Exit codes:
  0 success, 1 runtime failure, 2 bad flag.

Docs: docs/cli/models.md
`;

const HELP_BENCHMARKS = `tryaii benchmarks -- List registered benchmarks

Usage:
  tryaii benchmarks [options]

Print the standard benchmarks the router scores prompts against. Each line
shows the benchmark name, its normalization range, and a description.

Options:
  --json                Print the benchmarks as pretty-printed JSON

Examples:
  tryaii benchmarks
  tryaii benchmarks --json

Exit codes:
  0 success, 1 runtime failure, 2 bad flag.

Docs: docs/cli/benchmarks.md
`;

const HELP_SETUP = `tryaii setup -- Download the embedding model and warm centroids

Usage:
  tryaii setup [options]

One-time initialization: download the embedding model and load or generate
the benchmark centroids so the first real route/eval is fast. Optional --
the same work happens lazily on first use.

Options:
  --model <name>        Embedding model name (default all-MiniLM-L6-v2)

Examples:
  tryaii setup
  tryaii setup --model all-mpnet-base-v2

Exit codes:
  0 success, 1 runtime failure, 2 bad flag.

Docs: docs/cli/setup.md
`;

const HELP_REGENERATE = `tryaii regenerate -- Rebuild benchmark centroids

Usage:
  tryaii regenerate [options]

Force-regenerate the benchmark centroids from the bundled training queries,
overwriting the user cache. Unlike setup (which only builds what is missing),
regenerate always rebuilds -- use it after changing the embedding model.

Options:
  --model <name>        Embedding model to generate with (default all-MiniLM-L6-v2)

Examples:
  tryaii regenerate
  tryaii regenerate --model all-mpnet-base-v2

Exit codes:
  0 success, 1 runtime failure, 2 bad flag.

Docs: docs/cli/regenerate.md
`;

const HELP_CACHELINT = `tryaii cachelint -- Pre-flight prompt-cache analysis

Usage:
  tryaii cachelint <input.json | -> [options]

Analyze prompts BEFORE they are sent: 18 dynamic-content detectors, per-model
token floors for 7 providers, stable-prefix computation, and predicted
HIT/PARTIAL/MISS across request sequences. Runs locally -- nothing is called.

Arguments:
  <input.json>          Request JSON: an object with "prompt" and "llm"
                        ({"provider": ..., "name": ...}), a list of those, or
                        {"inputs": [...]}. Use '-' to read from stdin.

Options:
  --provider <name>     Raw-text mode: treat the ENTIRE input as one prompt
                        string for this provider (openai, anthropic, gemini,
                        xai, openrouter, bedrock, vertex -- aliases accepted)
  --model <name>        Model name for raw-text mode (requires --provider;
                        omit to use the provider's conservative default floor)
  --json                Emit the full machine-readable result instead of the
                        text report

Notes:
  cachelint warns, it never blocks: findings do not change the exit code.
  Exact OpenAI/xAI token counts use the o200k tokenizer -- install the extra
  on Python ('pip install tryaii[cachelint]'); the Node SDK bundles it.
  Thresholds/prices are time-sensitive; verify against live provider docs.

Examples:
  tryaii cachelint request.json
  tryaii cachelint requests.json --json
  cat prompt.txt | tryaii cachelint - --provider anthropic --model claude-fable-5

Exit codes:
  0 analysis completed (findings included), 1 runtime failure, 2 usage error
  or invalid input.

Docs: docs/cli/cachelint.md
`;

const HELP_DIAGNOSE = `tryaii diagnose -- Analyze a codebase's LLM call sites

Usage:
  tryaii diagnose <verb> [options]

diagnose is agent-first: your coding agent interviews you, finds the LLM
call sites in the codebase, and writes an inventory JSON; tryaii runs
deterministic checks over it and stores each run under .tryaii/diagnose/.
Insight-only -- it never edits code and never sends anything anywhere.

Verbs:
  plan                  Print the check catalog + interview for the agent (--json)
  check <inventory>     Run the checks over an agent-written inventory JSON

The four checks: model_fit (is each call site's model the right one for its
prompt under your priorities), cache_readiness (will the prompt hit the
provider's cache), cost_exposure (per-call/monthly cost, cache savings,
cheaper-swap suggestion), hygiene (prompt structure and dynamic-value
placement).

Examples:
  tryaii diagnose plan --json
  tryaii diagnose check inventory.json --quality 3 --cost 4 --speed 2

Exit codes:
  0 checks completed (findings included), 1 runtime failure, 2 usage error.

Docs: docs/cli/diagnose/README.md
`;

const HELP_DIAGNOSE_PLAN = `tryaii diagnose plan -- The check catalog + interview for the agent

Usage:
  tryaii diagnose plan [--json]

Prints what diagnose can check, the interview questions the agent should
ask the user (checks, scope, priorities, traffic, goal), and the inventory
shape the agent must produce. --json emits the machine-readable plan
(schema tryaii.diagnose.plan/1) including a complete inventory example --
agents should consume that.

Options:
  --json                Emit the machine-readable plan verbatim

Examples:
  tryaii diagnose plan
  tryaii diagnose plan --json

Exit codes:
  0 success, 2 usage error.

Docs: docs/cli/diagnose/plan.md
`;

const HELP_DIAGNOSE_CHECK = `tryaii diagnose check -- Run the checks over an inventory JSON

Usage:
  tryaii diagnose check <inventory.json | -> [options]

Reads an agent-written inventory of LLM call sites (see 'diagnose plan
--json' for the shape), runs the selected checks, and writes the run to
<out-dir>/<run-id>/ (inventory.json, findings.json, meta.json) plus a
'latest' pointer. Sites with missing data degrade honestly per check
('insufficient data' with a reason) -- nothing is guessed.

Requires 'tryaii setup' once beforehand when live classification is needed
(any site without a precomputed _classification).

Arguments:
  <inventory.json>      Inventory file, or '-' for stdin

Options:
  --quality <1-5>       Quality priority (default 3)
  --cost <1-5>          Cost priority (default 3)
  --speed <1-5>         Speed priority (default 3)
  --checks <list>       Comma-separated subset of model_fit, cache_readiness,
                        cost_exposure, hygiene (default: all)
  --calls-per-day <n>   Default traffic assumption for sites without one
  --output-tokens <n>   Default output tokens per call (default 500)
  --goal <text>         The user's stated goal (echoed into the findings)
  --out-dir <dir>       Run store directory (default .tryaii/diagnose)
  --run-id <id>         Override the run id (default: UTC timestamp)
  --now <iso8601>       Override the generated_at timestamp
  --json                Print the findings JSON to stdout instead of the summary
  --no-daemon           Classify in-process; do not use or start a daemon

Notes:
  diagnose warns, it never blocks: findings do not change the exit code.
  Cost figures are estimates; cache savings are an upper bound.

Examples:
  tryaii diagnose check inventory.json --quality 3 --cost 4 --speed 2
  tryaii diagnose check inventory.json --calls-per-day 1000 --goal "reduce prices"
  cat inventory.json | tryaii diagnose check - --json

Exit codes:
  0 checks completed (findings included), 1 runtime failure, 2 usage error
  or invalid input.

Docs: docs/cli/diagnose/check.md
`;

const HELP_HELP = `tryaii help -- Show help for tryaii or a specific command

Usage:
  tryaii help [command]
  tryaii <command> --help

With no argument, prints the global overview. With a command name, prints
detailed help for that command. The flags -h/--help after any command do
the same thing.

Topics:
  route, eval, cachelint, diagnose, models, benchmarks, setup, regenerate, help

Examples:
  tryaii help
  tryaii help eval
  tryaii route --help

Exit codes:
  0 success, 2 unknown help topic.

Docs: docs/cli/README.md
`;

/** Per-command help, keyed by command name. Mirrors COMMAND_HELP in the Python CLI. */
const COMMAND_HELP: Record<string, string> = {
  route: HELP_ROUTE,
  eval: HELP_EVAL,
  cachelint: HELP_CACHELINT,
  diagnose: HELP_DIAGNOSE,
  models: HELP_MODELS,
  benchmarks: HELP_BENCHMARKS,
  setup: HELP_SETUP,
  regenerate: HELP_REGENERATE,
  help: HELP_HELP,
};

/**
 * Per-verb help for the diagnose command. Mirrors DIAGNOSE_VERB_HELP in the
 * Python CLI (same parity guard as COMMAND_HELP).
 */
const DIAGNOSE_VERB_HELP: Record<string, string> = {
  plan: HELP_DIAGNOSE_PLAN,
  check: HELP_DIAGNOSE_CHECK,
};

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
  // subcommand (e.g. `tryaii eval --help`). All four git-style paths work:
  //   tryaii help            -> global overview
  //   tryaii help <command>  -> that command's detailed help
  //   tryaii <command> -h/--help -> that command's detailed help
  //   tryaii (bare)          -> global overview
  const wantsHelp = filtered.includes('-h') || filtered.includes('--help');

  if (command === 'help') {
    // First non-flag token is the topic. With no topic, bare `tryaii help`
    // prints the global overview, but `tryaii help -h/--help` documents the
    // help command itself -- consistent with `tryaii <command> --help`.
    const topic = subArgs.find((arg) => !arg.startsWith('-'));
    if (!topic) {
      await writePaced(wantsHelp ? COMMAND_HELP.help : HELP);
      return;
    }
    const topicHelp = COMMAND_HELP[topic];
    if (!topicHelp) {
      throw new CliUsageError(
        `unknown help topic: ${topic}. Run "tryaii help" for the list of commands.`,
      );
    }
    await writePaced(topicHelp);
    return;
  }

  if (!command) {
    await writePaced(HELP);
    return;
  }

  if (wantsHelp) {
    if (command === 'diagnose') {
      // `tryaii diagnose <verb> --help` gets the verb page.
      const verb = subArgs.find((arg) => !arg.startsWith('-'));
      await writePaced((verb && DIAGNOSE_VERB_HELP[verb]) || COMMAND_HELP.diagnose);
      return;
    }
    // Unknown command + --help still gets the global overview (then nothing else runs).
    await writePaced(COMMAND_HELP[command] ?? HELP);
    return;
  }

  switch (command) {
    case 'route':
      await cmdRoute(subArgs);
      break;
    case 'eval':
      await cmdEval(subArgs);
      break;
    case 'cachelint':
      await cmdCachelint(subArgs);
      break;
    case 'diagnose':
      await cmdDiagnose(subArgs);
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
