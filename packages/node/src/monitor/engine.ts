/**
 * The in-process routing engine of the Node monitor (TS-253): a Router over
 * the catalog bundle, in this process -- no Python, no daemon. Since H4 it
 * routes with the H4 router (`tryaii-router-h4`: a static model + head, pure
 * TypeScript) when `TRYAII_ROUTER` is `h4` (the default) and its payload
 * loads; otherwise with the int8 ONNX embedding model on onnxruntime-node and
 * that model's own centroids (the MiniLM fallback, as the Python SDK).
 *
 * It answers in the daemon's wire shape (`serializeRouteResult` +
 * `serializeEmbedding`), so the transport handles its results exactly like
 * a daemon's. Created by the transport's warm-up (off the hot path); when
 * onnxruntime-node or the model file is missing, `createEngine` returns the
 * reason instead and the transport falls back to the daemon (for one
 * release), then to metadata-only events.
 */

import type { CatalogBundle } from '../catalog/bundle.js';
import { usesH4, type TryaiiDreConfig } from '../config.js';
import {
  DEFAULT_ONNX_MODEL,
  DEFAULT_ROUTER_MODEL,
  ModelUnavailableError,
} from '../embeddings/modelStore.js';
import { OnnxEmbeddingProvider } from '../embeddings/onnx.js';
import type { Router } from '../router.js';
import { DEFAULT_PRIORITIES } from '../scoring/priorities.js';

export interface EngineResult {
  /** The daemon's `result` wire object (plus `embedding` when asked). */
  data: Record<string, unknown>;
}

export class InProcessEngine {
  readonly modelId: string;
  private readonly router: Router;
  private readonly topK: number;

  private constructor(router: Router, modelId: string, topK: number) {
    this.router = router;
    this.modelId = modelId;
    this.topK = topK;
  }

  /**
   * Build and warm the engine (loads the model, the centroids and the
   * difficulty exemplars). `{engine}` on success, `{reason}` otherwise;
   * never throws. H4 first when it applies (`modelId` is then the MiniLM
   * fallback); its unavailability is recorded in router-model.json by the
   * Router's selection.
   */
  static async create(
    config: TryaiiDreConfig,
    bundle: CatalogBundle,
    topK: number,
    modelId: string = DEFAULT_ONNX_MODEL,
  ): Promise<{ engine: InProcessEngine | null; reason: string | null }> {
    if (usesH4({ router: config.router, embeddingModel: modelId })) {
      try {
        const { Router } = await import('../router.js');
        const router = new Router({
          config: { ...config, embeddingModel: modelId },
          bundle,
          policy: null,
        });
        // Builds the classifier: H4, or (missing / broken payload, or a
        // catalog without its classes) the MiniLM centroid path below.
        if (router.routerId === DEFAULT_ROUTER_MODEL) {
          await router.route('warmup', { priorities: DEFAULT_PRIORITIES, topK: 1 });
          return { engine: new InProcessEngine(router, DEFAULT_ROUTER_MODEL, topK), reason: null };
        }
      } catch {
        /* fall back to MiniLM */
      }
    }
    try {
      const provider = new OnnxEmbeddingProvider(modelId);
      await provider.init();
      const { Router } = await import('../router.js');
      const router = new Router({
        config: { ...config, embeddingModel: modelId },
        embeddingProvider: provider,
        bundle,
        policy: null,
      });
      await router.route('warmup', { priorities: DEFAULT_PRIORITIES, topK: 1 });
      return { engine: new InProcessEngine(router, modelId, topK), reason: null };
    } catch (err) {
      if (err instanceof ModelUnavailableError) return { engine: null, reason: err.reason };
      return { engine: null, reason: err instanceof Error ? err.message : String(err) };
    }
  }

  async route(text: string, includeEmbedding: boolean, allowedModels: readonly string[] | null):
      Promise<EngineResult> {
    const { serializeEmbedding, serializeRouteResult } = await import('../server.js');
    const result = await this.router.route(text, {
      priorities: DEFAULT_PRIORITIES,
      topK: this.topK,
      allowedModels: allowedModels ?? null,
    });
    const data = serializeRouteResult(result);
    if (includeEmbedding) {
      data.embedding = serializeEmbedding(await this.router.embedPrompt(text), this.modelId);
    }
    return { data };
  }
}
