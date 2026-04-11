/**
 * DREClient -- unified high-level client for TryAii-DRE.
 *
 * Wraps model selection + OpenRouter API into a single class so users do not
 * need to manage separate objects. Selection is task-agnostic: models are
 * ranked by overall quality / cost / speed against the user's priorities --
 * the prompt itself is not classified. For prompt-aware semantic routing,
 * use the `tryaii-dre` core package directly.
 *
 * Usage:
 *   import { DREClient } from 'tryaii-dre-sdk';
 *
 *   const client = new DREClient({ apiKey: 'sk-or-...' });
 *   const response = await client.chat('Write a sorting algorithm');
 *   console.log(response.modelUsed, response.content);
 */

import type {
  ChatOptions,
  ChatResponse,
  DREClientOptions,
  ModelScore,
  RouteOptions,
  RouteResult,
  TokenUsage,
  Priorities as PrioritiesData,
} from './types.js';

// ---------------------------------------------------------------------------
// Model-ID to OpenRouter slug mapping
// ---------------------------------------------------------------------------

const MODEL_ID_TO_OPENROUTER: Record<string, string> = {
  // OpenAI
  'gpt-4o': 'openai/gpt-4o',
  'gpt-4o-mini': 'openai/gpt-4o-mini',
  'o1': 'openai/o1',
  'o3': 'openai/o3',
  'o4-mini': 'openai/o4-mini',
  'gpt-5': 'openai/gpt-5',
  'gpt-5-mini': 'openai/gpt-5-mini',
  'gpt-5.1': 'openai/gpt-5.1',
  'gpt-5.2': 'openai/gpt-5.2',
  'gpt-4.1': 'openai/gpt-4.1',
  'gpt-4.1-nano': 'openai/gpt-4.1-nano',
  'gpt-5-nano': 'openai/gpt-5-nano',
  // Anthropic
  'claude-3-7-sonnet-20250219': 'anthropic/claude-3.7-sonnet',
  'claude-sonnet-4-20250514': 'anthropic/claude-sonnet-4',
  'claude-sonnet-4-5-20250929': 'anthropic/claude-sonnet-4.5',
  'claude-haiku-4-5-20251001': 'anthropic/claude-haiku-4.5',
  'claude-opus-4-5-20251101': 'anthropic/claude-opus-4.5',
  // Google
  'gemini-2.5-pro': 'google/gemini-2.5-pro',
  'gemini-2.0-flash': 'google/gemini-2.0-flash',
  'gemini-2.5-flash': 'google/gemini-2.5-flash',
  'gemini-2.5-flash-lite': 'google/gemini-2.5-flash-lite',
  'gemini-3-pro-preview': 'google/gemini-3-pro-preview',
  'gemini-3-flash-preview': 'google/gemini-3-flash-preview',
  // DeepSeek
  'deepseek-reasoner': 'deepseek/deepseek-reasoner',
  'deepseek-chat': 'deepseek/deepseek-chat',
  // xAI
  'grok-3-latest': 'x-ai/grok-3',
  'grok-3-mini-latest': 'x-ai/grok-3-mini',
  'grok-4-latest': 'x-ai/grok-4',
  'grok-4-fast': 'x-ai/grok-4-fast',
  'grok-4-1-fast-reasoning-latest': 'x-ai/grok-4.1-fast-reasoning',
  'grok-code-fast': 'x-ai/grok-code-fast',
  // Mistral
  'mistral-large-latest': 'mistralai/mistral-large',
  'mistral-small-latest': 'mistralai/mistral-small',
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function resolveModel(modelId: string): string {
  return MODEL_ID_TO_OPENROUTER[modelId] ?? modelId;
}

const DEFAULT_PRIORITIES: PrioritiesData = { quality: 3, cost: 3, speed: 3 };

function mergePriorities(data?: PrioritiesData): PrioritiesData {
  if (!data) return DEFAULT_PRIORITIES;
  return {
    quality: Math.max(1, Math.min(5, Math.round(data.quality))),
    cost: Math.max(1, Math.min(5, Math.round(data.cost))),
    speed: Math.max(1, Math.min(5, Math.round(data.speed))),
  };
}

/** Convert priority 1-5 to weight used in scoring. */
function qualityWeight(p: PrioritiesData): number { return 0.3 + (p.quality / 5) * 0.9; }
function costWeight(p: PrioritiesData): number    { return 0.1 + (p.cost / 5) * 0.9; }
function speedWeight(p: PrioritiesData): number   { return 0.1 + (p.speed / 5) * 0.9; }

// ---------------------------------------------------------------------------
// Inline scoring engine (mirrors tryaii-dre ScoringEngine)
// ---------------------------------------------------------------------------

/** Normalization ranges for standard benchmarks. */
const NORMALIZATION_RANGES: Record<string, [number, number]> = {
  'MMLU': [25, 95],
  'HellaSwag': [50, 98],
  'HumanEval': [20, 95],
  'SWE-bench': [5, 85],
  'TruthfulQA': [20, 85],
  'ARC': [0, 95],
  'GSM8K': [20, 98],
  'DROP': [30, 90],
  'SuperGLUE': [40, 95],
  'Chatbot Arena (LMSys)': [1000, 1550],
  'MT-Bench': [5, 10],
  'LiveBench': [0, 100],
};

function normalizeBenchmark(benchmark: string, raw: number): number {
  const range = NORMALIZATION_RANGES[benchmark];
  if (!range) return Math.max(0, Math.min(1, raw / 100));
  const [min, max] = range;
  if (max === min) return 0.5;
  return Math.max(0, Math.min(1, (raw - min) / (max - min)));
}

/** Speed tier to numeric score. */
const SPEED_SCORES: Record<string, number> = {
  'very fast': 0.5,
  'fast': 0.4,
  'medium': 0.3,
  'slow': 0.2,
  'very slow': 0.1,
};

interface InternalModelData {
  modelId: string;
  provider: string;
  benchmarkScores: Record<string, number>;
  pricingInput?: number;
  pricingOutput?: number;
  latency?: string;
}

/**
 * Score models against the user's quality/cost/speed priorities.
 *
 * Quality is the unweighted mean of normalized benchmark scores across all
 * benchmarks the model has data for -- a task-agnostic measure of overall
 * capability. Cost and speed are computed from the model's pricing and
 * latency tier.
 *
 * The SDK does not classify the prompt before scoring; for prompt-aware
 * routing use the `tryaii-dre` core package directly.
 */
function scoreModels(
  models: InternalModelData[],
  priorities: PrioritiesData,
  topK: number,
): ModelScore[] {
  const qW = qualityWeight(priorities);
  const cW = costWeight(priorities);
  const sW = speedWeight(priorities);
  const totalWeight = qW + cW + sW;

  const scores: ModelScore[] = [];

  for (const model of models) {
    // Quality score: uniform mean of normalized benchmark scores.
    let normalizedSum = 0;
    let benchCount = 0;
    for (const [benchName, raw] of Object.entries(model.benchmarkScores)) {
      normalizedSum += normalizeBenchmark(benchName, raw);
      benchCount++;
    }
    if (benchCount === 0) continue;
    const qualityScore = normalizedSum / benchCount;

    // Cost score
    let costScore = 0;
    if (priorities.cost > 1 && model.pricingInput != null && model.pricingOutput != null) {
      const avg = (model.pricingInput + model.pricingOutput) / 2;
      costScore = Math.max(0, 1.0 - avg / 0.1);
    }

    // Speed score
    let speedScore = 0;
    if (priorities.speed > 1 && model.latency) {
      speedScore = SPEED_SCORES[model.latency] ?? 0.3;
    }

    // Combine
    const qContrib = qualityScore * qW;
    const cContrib = costScore * cW;
    const sContrib = speedScore * sW;
    let finalScore = (qContrib + cContrib + sContrib) / totalWeight;
    finalScore = Math.max(0, Math.min(1, finalScore));

    let reasoning = `Quality: ${qualityScore.toFixed(2)} (mean of ${benchCount} benchmarks)`;
    if (costScore > 0) reasoning += ` | Cost efficiency: ${costScore.toFixed(2)}`;
    if (speedScore > 0) reasoning += ` | Speed: ${speedScore.toFixed(2)} (${model.latency})`;

    scores.push({
      modelId: model.modelId,
      finalScore: Math.round(finalScore * 10000) / 10000,
      qualityScore: Math.round(qualityScore * 10000) / 10000,
      costScore: Math.round(costScore * 10000) / 10000,
      speedScore: Math.round(speedScore * 10000) / 10000,
      reasoning,
    });
  }

  // Sort descending
  scores.sort((a, b) => b.finalScore - a.finalScore);

  // Normalize to 0.1-0.95 range
  if (scores.length > 0) {
    const maxRaw = scores[0].finalScore;
    const minRaw = scores.length > 1 ? scores[scores.length - 1].finalScore : 0;

    for (const s of scores) {
      if (maxRaw === minRaw) {
        s.finalScore = 0.5;
      } else {
        const norm = (s.finalScore - minRaw) / (maxRaw - minRaw);
        s.finalScore = Math.round((0.1 + 0.85 * norm) * 10000) / 10000;
      }
    }
  }

  return scores.slice(0, topK);
}

// ---------------------------------------------------------------------------
// Bundled model registry (default models)
// ---------------------------------------------------------------------------

/** Load models from the tryaii-dre default preset JSON. */
async function loadDefaultModels(): Promise<InternalModelData[]> {
  // Try to load from the tryaii-dre package's preset file
  try {
    const { readFileSync } = await import('node:fs');
    const { dirname, join } = await import('node:path');
    const { createRequire } = await import('node:module');

    // Attempt to resolve tryaii-dre package location
    const require = createRequire(import.meta.url);
    let presetsDir: string;
    try {
      const corePkg = require.resolve('tryaii-dre/package.json');
      presetsDir = join(dirname(corePkg), 'src', 'registry', 'presets');
    } catch {
      // Fallback: navigate up from SDK location to sibling node package
      const sdkDir = dirname(dirname(new URL(import.meta.url).pathname));
      presetsDir = join(sdkDir, '..', '..', 'node', 'src', 'registry', 'presets');
    }

    const raw = readFileSync(join(presetsDir, 'defaultModels.json'), 'utf-8');
    const data = JSON.parse(raw) as {
      models: Array<{
        model_id: string;
        provider: string;
        benchmark_scores?: Record<string, number | null>;
        pricing?: { input_per_1k: number; output_per_1k: number } | null;
        latency?: string | null;
      }>;
    };

    return data.models.map((m) => {
      const benchmarks: Record<string, number> = {};
      if (m.benchmark_scores) {
        for (const [k, v] of Object.entries(m.benchmark_scores)) {
          if (v != null) benchmarks[k] = v;
        }
      }
      return {
        modelId: m.model_id,
        provider: m.provider,
        benchmarkScores: benchmarks,
        pricingInput: m.pricing?.input_per_1k,
        pricingOutput: m.pricing?.output_per_1k,
        latency: m.latency ?? undefined,
      };
    });
  } catch {
    // Fallback: return a minimal set of well-known models
    return FALLBACK_MODELS;
  }
}

/** Minimal fallback models if the preset JSON cannot be loaded. */
const FALLBACK_MODELS: InternalModelData[] = [
  {
    modelId: 'gpt-4o',
    provider: 'openai',
    benchmarkScores: { MMLU: 88.7, HumanEval: 90.2, GSM8K: 95.8, HellaSwag: 95.3, 'Chatbot Arena (LMSys)': 1310, 'MT-Bench': 9.3 },
    pricingInput: 0.0025, pricingOutput: 0.01, latency: 'fast',
  },
  {
    modelId: 'gpt-4o-mini',
    provider: 'openai',
    benchmarkScores: { MMLU: 82.0, HumanEval: 87.0, GSM8K: 93.0, HellaSwag: 89.0, 'Chatbot Arena (LMSys)': 1240, 'MT-Bench': 8.7 },
    pricingInput: 0.00015, pricingOutput: 0.0006, latency: 'very fast',
  },
  {
    modelId: 'gpt-5',
    provider: 'openai',
    benchmarkScores: { MMLU: 93.5, HumanEval: 95.8, GSM8K: 98.0, HellaSwag: 96.5, 'Chatbot Arena (LMSys)': 1410, 'MT-Bench': 9.6, 'SWE-bench': 72.0, DROP: 88.5 },
    pricingInput: 0.01, pricingOutput: 0.03, latency: 'medium',
  },
  {
    modelId: 'claude-sonnet-4-5-20250929',
    provider: 'anthropic',
    benchmarkScores: { MMLU: 91.0, HumanEval: 94.0, GSM8K: 96.5, HellaSwag: 95.0, 'Chatbot Arena (LMSys)': 1380, 'MT-Bench': 9.5, 'SWE-bench': 68.0 },
    pricingInput: 0.003, pricingOutput: 0.015, latency: 'fast',
  },
  {
    modelId: 'claude-haiku-4-5-20251001',
    provider: 'anthropic',
    benchmarkScores: { MMLU: 84.0, HumanEval: 88.5, GSM8K: 92.5, HellaSwag: 90.0, 'Chatbot Arena (LMSys)': 1260, 'MT-Bench': 8.9 },
    pricingInput: 0.0008, pricingOutput: 0.004, latency: 'very fast',
  },
  {
    modelId: 'gemini-2.5-pro',
    provider: 'google',
    benchmarkScores: { MMLU: 90.5, HumanEval: 92.0, GSM8K: 96.0, HellaSwag: 94.5, 'Chatbot Arena (LMSys)': 1370, 'MT-Bench': 9.4, 'SWE-bench': 65.0 },
    pricingInput: 0.00125, pricingOutput: 0.005, latency: 'fast',
  },
  {
    modelId: 'gemini-2.5-flash',
    provider: 'google',
    benchmarkScores: { MMLU: 86.0, HumanEval: 89.0, GSM8K: 94.5, HellaSwag: 91.5, 'Chatbot Arena (LMSys)': 1290, 'MT-Bench': 9.0 },
    pricingInput: 0.00015, pricingOutput: 0.0006, latency: 'very fast',
  },
  {
    modelId: 'deepseek-chat',
    provider: 'deepseek',
    benchmarkScores: { MMLU: 87.0, HumanEval: 89.5, GSM8K: 94.0, HellaSwag: 92.0, 'Chatbot Arena (LMSys)': 1280, 'MT-Bench': 8.8 },
    pricingInput: 0.00014, pricingOutput: 0.00028, latency: 'fast',
  },
  {
    modelId: 'deepseek-reasoner',
    provider: 'deepseek',
    benchmarkScores: { MMLU: 90.0, HumanEval: 93.0, GSM8K: 97.0, HellaSwag: 94.0, 'Chatbot Arena (LMSys)': 1360, 'MT-Bench': 9.3, DROP: 86.0 },
    pricingInput: 0.00055, pricingOutput: 0.0022, latency: 'slow',
  },
  {
    modelId: 'grok-4-latest',
    provider: 'xai',
    benchmarkScores: { MMLU: 91.0, HumanEval: 93.5, GSM8K: 96.0, HellaSwag: 95.0, 'Chatbot Arena (LMSys)': 1390, 'MT-Bench': 9.4 },
    pricingInput: 0.003, pricingOutput: 0.015, latency: 'medium',
  },
];

// ---------------------------------------------------------------------------
// DREClient
// ---------------------------------------------------------------------------

export class DREClient {
  private readonly _apiKey: string;
  private readonly _baseUrl: string;
  private readonly _defaultPriorities: PrioritiesData;
  private _models: InternalModelData[] | null = null;

  constructor(options?: DREClientOptions) {
    this._apiKey = options?.apiKey ?? process.env.OPENROUTER_API_KEY ?? '';
    this._baseUrl = options?.baseUrl ?? 'https://openrouter.ai/api/v1';
    this._defaultPriorities = mergePriorities(options?.priorities);
  }

  /** Lazy-load the model registry. */
  private async _ensureModels(): Promise<InternalModelData[]> {
    if (this._models) return this._models;
    this._models = await loadDefaultModels();
    return this._models;
  }

  /** Sync access to models (uses fallback if not yet loaded). */
  private _modelsSync(): InternalModelData[] {
    return this._models ?? FALLBACK_MODELS;
  }

  // -----------------------------------------------------------------------
  // route -- sync, no API call
  // -----------------------------------------------------------------------

  /**
   * Pick the best model for the user's priorities, without making an API call.
   *
   * Routing in the SDK is task-agnostic: models are ranked by overall quality
   * (mean of normalized benchmark scores), cost, and speed -- the prompt
   * itself is not classified. Use the `tryaii-dre` core package directly if
   * you need prompt-aware semantic routing.
   *
   * The `prompt` argument is accepted for API compatibility but is currently
   * unused by the routing logic.
   */
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  route(prompt: string, options?: RouteOptions): RouteResult {
    const priorities = mergePriorities(options?.priorities ?? this._defaultPriorities);
    const topK = options?.topK ?? 5;

    const models = this._modelsSync();
    const scores = scoreModels(models, priorities, topK);

    return {
      bestModel: scores[0]?.modelId ?? '',
      scores,
      bestScore: scores[0]?.finalScore ?? 0,
      bestReasoning: scores[0]?.reasoning ?? '',
      priorities,
    };
  }

  // -----------------------------------------------------------------------
  // chat -- async, makes API call
  // -----------------------------------------------------------------------

  /**
   * Pick the best model for the user's priorities and return the AI response.
   *
   * Routing is task-agnostic (see `route` for details).
   */
  async chat(prompt: string, options?: ChatOptions): Promise<ChatResponse> {
    const priorities = mergePriorities(options?.priorities ?? this._defaultPriorities);
    const models = await this._ensureModels();

    // Pick best model by priorities (no prompt classification)
    const scores = scoreModels(models, priorities, 5);

    const modelId = scores[0]?.modelId ?? 'gpt-4o';
    const reasoning = scores[0]?.reasoning ?? '';
    const openrouterModel = resolveModel(modelId);

    // Build messages
    const messages: Array<{ role: string; content: string }> = [];
    if (options?.systemMessage) {
      messages.push({ role: 'system', content: options.systemMessage });
    }
    messages.push({ role: 'user', content: prompt });

    // Build payload
    const payload: Record<string, unknown> = {
      model: openrouterModel,
      messages,
      temperature: options?.temperature ?? 0.7,
    };
    if (options?.maxTokens) {
      payload.max_tokens = options.maxTokens;
    }

    // Call OpenRouter API
    const response = await fetch(`${this._baseUrl}/chat/completions`, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${this._apiKey}`,
        'Content-Type': 'application/json',
        'X-Title': 'tryaii-dre',
      },
      body: JSON.stringify(payload),
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`OpenRouter API error (${response.status}): ${errorText}`);
    }

    const data = await response.json() as Record<string, unknown>;
    const choices = data.choices as Array<{ message: { content: string } }>;
    const content = choices?.[0]?.message?.content ?? '';

    const rawUsage = data.usage as Record<string, number> | undefined;
    const usage: TokenUsage = {
      promptTokens: rawUsage?.prompt_tokens,
      completionTokens: rawUsage?.completion_tokens,
      totalTokens: rawUsage?.total_tokens,
    };

    return {
      content,
      modelUsed: modelId,
      openrouterModel,
      routeReasoning: reasoning,
      usage,
      rawResponse: data,
    };
  }

  // -----------------------------------------------------------------------
  // stream -- async generator
  // -----------------------------------------------------------------------

  /**
   * Pick the best model for the user's priorities and stream the response.
   *
   * Routing is task-agnostic (see `route` for details). Yields content chunks
   * as they arrive from the API.
   */
  async *stream(prompt: string, options?: ChatOptions): AsyncGenerator<string> {
    const priorities = mergePriorities(options?.priorities ?? this._defaultPriorities);
    const models = await this._ensureModels();

    // Pick best model by priorities (no prompt classification)
    const scores = scoreModels(models, priorities, 5);

    const modelId = scores[0]?.modelId ?? 'gpt-4o';
    const openrouterModel = resolveModel(modelId);

    // Build messages
    const messages: Array<{ role: string; content: string }> = [];
    if (options?.systemMessage) {
      messages.push({ role: 'system', content: options.systemMessage });
    }
    messages.push({ role: 'user', content: prompt });

    // Build payload
    const payload: Record<string, unknown> = {
      model: openrouterModel,
      messages,
      temperature: options?.temperature ?? 0.7,
      stream: true,
    };
    if (options?.maxTokens) {
      payload.max_tokens = options.maxTokens;
    }

    // Call OpenRouter API with streaming
    const response = await fetch(`${this._baseUrl}/chat/completions`, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${this._apiKey}`,
        'Content-Type': 'application/json',
        'X-Title': 'tryaii-dre',
      },
      body: JSON.stringify(payload),
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`OpenRouter API error (${response.status}): ${errorText}`);
    }

    if (!response.body) {
      throw new Error('Response body is null -- streaming not supported');
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() ?? '';

        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed || !trimmed.startsWith('data: ')) continue;

          const dataStr = trimmed.slice(6);
          if (dataStr === '[DONE]') return;

          try {
            const chunk = JSON.parse(dataStr) as {
              choices: Array<{ delta: { content?: string } }>;
            };
            const content = chunk.choices?.[0]?.delta?.content;
            if (content) yield content;
          } catch {
            // Skip malformed SSE chunks
          }
        }
      }
    } finally {
      reader.releaseLock();
    }
  }
}
