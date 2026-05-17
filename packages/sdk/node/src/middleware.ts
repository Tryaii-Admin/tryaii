/**
 * Express middleware for TryAii-DRE.
 *
 * Adds routing headers to responses so downstream consumers know which
 * model was selected and its confidence score.
 *
 * Usage:
 *   import express from 'express';
 *   import { dreMiddleware } from 'tryaii-dre-sdk/middleware';
 *
 *   const app = express();
 *   app.use(express.json());
 *   app.use(dreMiddleware({ priorities: { quality: 5, cost: 1, speed: 3 } }));
 */

import type { Request, Response, NextFunction, RequestHandler } from 'express';
import type { DREMiddlewareOptions } from './types.js';
import { DREClient } from './client.js';

/**
 * Create Express middleware that classifies the incoming request body
 * and attaches routing headers to the response.
 *
 * Headers added (default prefix "X-DRE"):
 *   - X-DRE-Model: the recommended model ID
 *   - X-DRE-Score: the model's final score (0-1)
 */
export function dreMiddleware(options?: DREMiddlewareOptions): RequestHandler {
  const prefix = options?.headerPrefix ?? 'X-DRE';
  const promptField = options?.promptField ?? 'prompt';
  const onError = options?.onError;

  const client = new DREClient({
    apiKey: options?.apiKey,
    priorities: options?.priorities,
  });

  return async (req: Request, res: Response, next: NextFunction): Promise<void> => {
    try {
      const body = req.body as Record<string, unknown> | undefined;
      const prompt = body?.[promptField];

      if (typeof prompt === 'string' && prompt.length > 0) {
        const result = await client.route(prompt, {
          priorities: options?.priorities,
        });

        res.setHeader(`${prefix}-Model`, result.bestModel);
        res.setHeader(`${prefix}-Score`, String(result.bestScore));
      }
    } catch (err) {
      // Routing failure must not block the request pipeline. The onError hook
      // is the only way to observe these failures in production; without it
      // they are silently swallowed.
      if (onError) {
        try {
          onError(err);
        } catch {
          // A broken onError hook must also not block the pipeline.
        }
      }
    }

    next();
  };
}
