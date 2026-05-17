/**
 * SDK-specific TypeScript types for TryAii-DRE.
 *
 * These types define the public API surface of the Node SDK.
 */

/**
 * User priorities for model selection.
 *
 * Each value is on a 1-5 scale:
 *   1 = don't care about this dimension
 *   3 = balanced (default)
 *   5 = this is critical
 */
export interface Priorities {
  quality: number;
  cost: number;
  speed: number;
}

/**
 * A single model's score from the routing engine.
 */
export interface ModelScore {
  modelId: string;
  finalScore: number;
  qualityScore: number;
  costScore: number;
  speedScore: number;
  reasoning: string;
}

/**
 * Result of routing a prompt (no API call made).
 */
export interface RouteResult {
  /** The top recommended model ID. */
  bestModel: string;

  /** All scored models, sorted by score descending. */
  scores: ModelScore[];

  /** Score of the best model (convenience accessor). */
  bestScore: number;

  /** Reasoning for why the top model was chosen. */
  bestReasoning: string;

  /** The priorities that were used for this routing decision. */
  priorities: Priorities;
}

/**
 * Response from a chat API call.
 */
export interface ChatResponse {
  /** The AI-generated response text. */
  content: string;

  /** TryAii-DRE model ID that was used. */
  modelUsed: string;

  /** OpenRouter model slug that was called. */
  openrouterModel: string;

  /** Reasoning for why this model was chosen. */
  routeReasoning: string;

  /** Token usage information. */
  usage: TokenUsage;

  /** Raw API response data. */
  rawResponse?: Record<string, unknown>;
}

/**
 * Token usage from an API call.
 */
export interface TokenUsage {
  promptTokens?: number;
  completionTokens?: number;
  totalTokens?: number;
}

/**
 * Options for chat and stream calls.
 */
export interface ChatOptions {
  /** Override default priorities for this call. */
  priorities?: Priorities;

  /** System message / system prompt. */
  systemMessage?: string;

  /** Sampling temperature (0.0 to 2.0). Default: 0.7. */
  temperature?: number;

  /** Maximum tokens in the response. */
  maxTokens?: number;
}

/**
 * Options for route-only calls.
 */
export interface RouteOptions {
  /** Override default priorities for this call. */
  priorities?: Priorities;

  /** Number of top models to return. Default: 5. */
  topK?: number;
}

/**
 * Constructor options for DREClient.
 */
export interface DREClientOptions {
  /** OpenRouter API key. Falls back to OPENROUTER_API_KEY env var. */
  apiKey?: string;

  /** Default priorities for all routing calls. */
  priorities?: Priorities;

  /** Base URL for OpenRouter API. */
  baseUrl?: string;
}

/**
 * Options for Express middleware.
 */
export interface DREMiddlewareOptions {
  /** OpenRouter API key. Falls back to OPENROUTER_API_KEY env var. Not required for routing-only middleware. */
  apiKey?: string;

  /** Default priorities for routing. */
  priorities?: Priorities;

  /** Prefix for response headers. Default: "X-DRE". */
  headerPrefix?: string;

  /** JSON body field to extract the prompt from. Default: "prompt". */
  promptField?: string;

  /**
   * Callback invoked when routing fails inside the middleware.
   *
   * Routing failures never block the request pipeline — `next()` always runs.
   * This hook is the only way to observe/log such failures in production.
   * If omitted, failures are silently swallowed.
   */
  onError?: (error: unknown) => void;
}
