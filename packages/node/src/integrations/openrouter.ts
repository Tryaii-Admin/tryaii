/**
 * OpenRouter integration -- active routing through OpenRouter's API.
 *
 * Wraps OpenRouter API calls so that TryAii-DRE automatically selects
 * the best model based on the prompt, then forwards the request.
 *
 * Uses the native fetch() API (available in Node 18+).
 *
 * Usage:
 *   import { Router } from 'tryaii-dre';
 *   import { OpenRouterIntegration } from 'tryaii-dre/integrations';
 *
 *   const router = new Router();
 *   const openrouter = new OpenRouterIntegration(router, { apiKey: 'sk-or-...' });
 *
 *   const response = await openrouter.chat('Write a Python quicksort implementation');
 *   console.log(response.modelUsed);
 *   console.log(response.content);
 */

import type { Priorities, PrioritiesData } from '../scoring/priorities.js';

/** Mapping from our model IDs to OpenRouter model slugs. */
export const MODEL_ID_TO_OPENROUTER: Record<string, string> = {
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

/** Response from an OpenRouter API call. */
export interface OpenRouterResponse {
  /** The response text content. */
  content: string;
  /** TryAii-DRE model ID that was selected. */
  modelUsed: string;
  /** OpenRouter model slug (e.g., "openai/gpt-4o"). */
  openrouterModel: string;
  /** Why this model was chosen. */
  routeReasoning: string;
  /** Token usage information. */
  usage: Record<string, unknown>;
  /** Raw API response. */
  rawResponse: Record<string, unknown> | null;
}

/** Options for chat() and stream() calls. */
export interface OpenRouterChatOptions {
  /** Optional priority dict. */
  priorities?: Partial<PrioritiesData>;
  /** Optional system prompt. */
  systemMessage?: string;
  /** Sampling temperature (default: 0.7). */
  temperature?: number;
  /** Maximum response tokens. */
  maxTokens?: number;
  /** Skip routing and use this model directly. */
  overrideModel?: string;
}

/**
 * Active routing integration with OpenRouter.
 *
 * Combines TryAii-DRE's semantic routing with OpenRouter's multi-provider
 * API to automatically select and call the best model.
 */
export class OpenRouterIntegration {
  static readonly OPENROUTER_BASE_URL = 'https://openrouter.ai/api/v1';

  // Use `any` to avoid circular import with Router
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private _router: any;
  private _apiKey: string;
  private _appName: string;

  constructor(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    router: any,
    opts?: {
      apiKey?: string;
      appName?: string;
    },
  ) {
    this._router = router;
    this._apiKey = opts?.apiKey ?? '';
    this._appName = opts?.appName ?? 'tryaii-dre';
  }

  private _ensureApiKey(): void {
    if (!this._apiKey) {
      throw new Error('OpenRouterIntegration requires an explicit apiKey');
    }
  }

  /** Convert TryAii-DRE model ID to OpenRouter slug. */
  private _resolveModel(modelId: string): string {
    return MODEL_ID_TO_OPENROUTER[modelId] ?? modelId;
  }

  /**
   * Route and complete a chat request.
   *
   * @param prompt - User message.
   * @param opts - Optional configuration (priorities, temperature, etc.).
   * @returns OpenRouterResponse with content and routing info.
   */
  async chat(prompt: string, opts?: OpenRouterChatOptions): Promise<OpenRouterResponse> {
    this._ensureApiKey();

    let modelId: string;
    let reasoning: string;

    if (opts?.overrideModel) {
      modelId = opts.overrideModel;
      reasoning = `Model override: ${opts.overrideModel}`;
    } else {
      const { Priorities } = await import('../scoring/priorities.js');
      const prio: Priorities | undefined = opts?.priorities
        ? Priorities.fromDict(opts.priorities)
        : undefined;
      const routeResult = await this._router.route(prompt, { priorities: prio });
      modelId = routeResult.bestModel;
      reasoning = routeResult.scores[0]?.reasoning ?? '';
    }

    const openrouterModel = this._resolveModel(modelId);

    // Build messages
    const messages: Array<{ role: string; content: string }> = [];
    if (opts?.systemMessage) {
      messages.push({ role: 'system', content: opts.systemMessage });
    }
    messages.push({ role: 'user', content: prompt });

    // Build payload
    const payload: Record<string, unknown> = {
      model: openrouterModel,
      messages,
      temperature: opts?.temperature ?? 0.7,
    };
    if (opts?.maxTokens) {
      payload.max_tokens = opts.maxTokens;
    }

    // Make API call using native fetch
    const response = await fetch(`${OpenRouterIntegration.OPENROUTER_BASE_URL}/chat/completions`, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${this._apiKey}`,
        'X-Title': this._appName,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
    });

    if (!response.ok) {
      throw new Error(`OpenRouter API error: ${response.status} ${response.statusText}`);
    }

    const data = await response.json() as Record<string, any>;
    const content = data.choices?.[0]?.message?.content ?? '';
    const usage = data.usage ?? {};

    return {
      content,
      modelUsed: modelId,
      openrouterModel,
      routeReasoning: reasoning,
      usage,
      rawResponse: data as Record<string, unknown>,
    };
  }

  /**
   * Route and stream a chat response.
   *
   * Yields content chunks as they arrive.
   */
  async *stream(prompt: string, opts?: OpenRouterChatOptions): AsyncGenerator<string> {
    this._ensureApiKey();

    let modelId: string;

    if (opts?.overrideModel) {
      modelId = opts.overrideModel;
    } else {
      const { Priorities } = await import('../scoring/priorities.js');
      const prio: Priorities | undefined = opts?.priorities
        ? Priorities.fromDict(opts.priorities)
        : undefined;
      const routeResult = await this._router.route(prompt, { priorities: prio });
      modelId = routeResult.bestModel;
    }

    const openrouterModel = this._resolveModel(modelId);

    const messages: Array<{ role: string; content: string }> = [];
    if (opts?.systemMessage) {
      messages.push({ role: 'system', content: opts.systemMessage });
    }
    messages.push({ role: 'user', content: prompt });

    const payload: Record<string, unknown> = {
      model: openrouterModel,
      messages,
      temperature: opts?.temperature ?? 0.7,
      stream: true,
    };
    if (opts?.maxTokens) {
      payload.max_tokens = opts.maxTokens;
    }

    const response = await fetch(`${OpenRouterIntegration.OPENROUTER_BASE_URL}/chat/completions`, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${this._apiKey}`,
        'X-Title': this._appName,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
    });

    if (!response.ok) {
      throw new Error(`OpenRouter API error: ${response.status} ${response.statusText}`);
    }

    if (!response.body) {
      throw new Error('Response body is null');
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() ?? '';

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const dataStr = line.slice(6).trim();
        if (dataStr === '[DONE]') return;

        try {
          const chunk = JSON.parse(dataStr);
          const content = chunk.choices?.[0]?.delta?.content ?? '';
          if (content) yield content;
        } catch {
          continue;
        }
      }
    }
  }
}
