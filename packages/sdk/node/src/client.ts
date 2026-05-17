/**
 * DREClient -- unified high-level client for TryAii-DRE.
 *
 * Wraps prompt-aware model selection (via the `tryaii-dre` core `Router`) and
 * the OpenRouter API into a single class so users do not have to manage
 * separate objects.
 *
 * The SDK delegates routing to the core `Router`, which classifies prompts
 * against benchmark centroids using embeddings -- so selection here IS
 * prompt-aware. The SDK then forwards the chosen model to OpenRouter for
 * completion.
 *
 * Usage:
 *   import { DREClient } from 'tryaii-dre-sdk';
 *
 *   const client = new DREClient({ apiKey: 'sk-or-...' });
 *   const response = await client.chat('Write a sorting algorithm');
 *   console.log(response.modelUsed, response.content);
 */

import { Router, Priorities, MODEL_ID_TO_OPENROUTER } from 'tryaii-dre';

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
// Helpers
// ---------------------------------------------------------------------------

const DEFAULT_PRIORITIES: PrioritiesData = { quality: 3, cost: 3, speed: 3 };

function mergePriorities(data?: PrioritiesData): PrioritiesData {
  if (!data) return DEFAULT_PRIORITIES;
  return {
    quality: Math.max(1, Math.min(5, Math.round(data.quality))),
    cost: Math.max(1, Math.min(5, Math.round(data.cost))),
    speed: Math.max(1, Math.min(5, Math.round(data.speed))),
  };
}

function resolveModel(modelId: string): string {
  return MODEL_ID_TO_OPENROUTER[modelId] ?? modelId;
}

// ---------------------------------------------------------------------------
// DREClient
// ---------------------------------------------------------------------------

export class DREClient {
  private readonly _apiKey: string;
  private readonly _baseUrl: string;
  private readonly _defaultPriorities: PrioritiesData;
  private readonly _router: Router;

  constructor(options?: DREClientOptions) {
    this._apiKey = options?.apiKey ?? process.env.OPENROUTER_API_KEY ?? '';
    this._baseUrl = options?.baseUrl ?? 'https://openrouter.ai/api/v1';
    this._defaultPriorities = mergePriorities(options?.priorities);
    this._router = new Router();
  }

  /** Throw early with a clear message when chat/stream is called without an API key. */
  private _ensureApiKey(): void {
    if (!this._apiKey) {
      throw new Error(
        'DREClient requires an OpenRouter API key. Pass { apiKey } to the constructor ' +
          'or set the OPENROUTER_API_KEY environment variable.',
      );
    }
  }

  // -----------------------------------------------------------------------
  // route -- async, no API call
  // -----------------------------------------------------------------------

  /**
   * Pick the best model for the prompt and the user's priorities, without
   * making an API call.
   *
   * Routing is prompt-aware: the core `Router` embeds the prompt and matches
   * it against benchmark centroids before scoring models. Async because the
   * default embedding provider (`@xenova/transformers`) is async-only.
   */
  async route(prompt: string, options?: RouteOptions): Promise<RouteResult> {
    const priorities = mergePriorities(options?.priorities ?? this._defaultPriorities);
    const topK = options?.topK ?? 5;

    const coreResult = await this._router.route(prompt, {
      priorities: Priorities.fromDict(priorities),
      topK,
    });

    return this._toSdkResult(coreResult, priorities);
  }

  // -----------------------------------------------------------------------
  // chat -- async, makes API call
  // -----------------------------------------------------------------------

  /**
   * Pick the best model for the prompt and return the AI response.
   */
  async chat(prompt: string, options?: ChatOptions): Promise<ChatResponse> {
    this._ensureApiKey();
    const priorities = mergePriorities(options?.priorities ?? this._defaultPriorities);

    const coreResult = await this._router.route(prompt, {
      priorities: Priorities.fromDict(priorities),
    });

    if (!coreResult.bestModel) {
      throw new Error(
        'DREClient.chat: routing returned no model -- the registry is empty or all models were filtered out',
      );
    }
    const modelId = coreResult.bestModel;
    const reasoning = coreResult.scores[0]?.reasoning ?? '';
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
   * Pick the best model for the prompt and stream the response.
   *
   * Yields content chunks as they arrive from the API.
   */
  async *stream(prompt: string, options?: ChatOptions): AsyncGenerator<string> {
    this._ensureApiKey();
    const priorities = mergePriorities(options?.priorities ?? this._defaultPriorities);

    const coreResult = await this._router.route(prompt, {
      priorities: Priorities.fromDict(priorities),
    });

    if (!coreResult.bestModel) {
      throw new Error(
        'DREClient.stream: routing returned no model -- the registry is empty or all models were filtered out',
      );
    }
    const modelId = coreResult.bestModel;
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

  // -----------------------------------------------------------------------
  // internal: convert core RouteResult to SDK shape
  // -----------------------------------------------------------------------

  private _toSdkResult(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    coreResult: any,
    priorities: PrioritiesData,
  ): RouteResult {
    const scores: ModelScore[] = (coreResult.scores ?? []).map(
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (s: any): ModelScore => ({
        modelId: s.modelId,
        finalScore: s.finalScore,
        qualityScore: s.qualityScore,
        costScore: s.costScore,
        speedScore: s.speedScore,
        reasoning: s.reasoning,
      }),
    );

    return {
      bestModel: coreResult.bestModel ?? '',
      scores,
      bestScore: scores[0]?.finalScore ?? 0,
      bestReasoning: scores[0]?.reasoning ?? '',
      priorities,
    };
  }
}
