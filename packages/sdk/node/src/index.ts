/**
 * TryAii-DRE Node SDK
 *
 * High-level client with async support and Express middleware
 * for semantic AI model routing.
 *
 * Usage:
 *   import { DREClient } from 'tryaii-dre-sdk';
 *
 *   const client = new DREClient();
 *   const response = await client.chat("Write a sorting algorithm");
 *   console.log(response.modelUsed, response.content);
 */

export { DREClient } from './client.js';
export { dreMiddleware } from './middleware.js';

// Re-export all public types
export type {
  Priorities,
  ModelScore,
  RouteResult,
  ChatResponse,
  TokenUsage,
  ChatOptions,
  RouteOptions,
  DREClientOptions,
  DREMiddlewareOptions,
} from './types.js';

// HTML report generator for eval runs (re-exported from tryaii-dre)
export { renderDashboard } from 'tryaii-dre';
export type { DashboardSummary, DashboardLinks } from 'tryaii-dre';
