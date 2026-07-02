/**
 * High-level client TypeScript types for TryAii.
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
 * Result of routing a prompt, with no API call made.
 */
export interface RouteResult {
  /** The top recommended model ID. */
  bestModel: string;

  /** All scored models, sorted by score descending. */
  scores: ModelScore[];

  /** Score of the best model. */
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

  /** TryAii model ID that was used. */
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

  /** Sampling temperature. Default: 0.7. */
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
