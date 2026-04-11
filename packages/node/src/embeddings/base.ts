/**
 * Abstract embedding provider interface.
 *
 * Allows swapping between local (@xenova/transformers) and cloud (OpenAI)
 * embedding backends without changing the classifier logic.
 *
 * The async path (`embedAsync` / `embedBatchAsync`) is the primary contract
 * -- every provider must implement it. Sync support is optional: providers
 * that can produce embeddings without I/O can override `embed` / `embedBatch`
 * and set `supportsSync` to true. Sync providers automatically support the
 * async path through the default fallback below.
 */

/**
 * Base class for embedding providers.
 *
 * All providers must implement embedAsync(), embedBatchAsync(), dimension,
 * and modelName. Sync support is optional and gated on `supportsSync`.
 */
export abstract class BaseEmbeddingProvider {
  /**
   * Generate an embedding vector for a single text (async).
   *
   * Default implementation wraps the sync `embed()` -- so sync providers
   * (those that override `embed`) automatically support the async path
   * with no extra code. Async-only providers must override this method
   * and leave `embed()` to its default-throwing implementation.
   */
  async embedAsync(text: string): Promise<number[]> {
    return this.embed(text);
  }

  /**
   * Generate embeddings for multiple texts (async).
   *
   * Default implementation loops `embedAsync()` -- which routes through the
   * provider's real async implementation for async-only providers, and
   * through the sync-wrapping default for sync providers. Override for
   * batch-friendly async backends that can do better than N sequential calls.
   */
  async embedBatchAsync(texts: string[]): Promise<number[][]> {
    const out: number[][] = [];
    for (const t of texts) out.push(await this.embedAsync(t));
    return out;
  }

  /**
   * Generate an embedding vector synchronously.
   *
   * Optional. Default throws -- only providers that have set
   * `supportsSync = true` should override this. Used by the niche
   * `Router.routeSync()` path for callers that need a blocking API.
   */
  embed(_text: string): number[] {
    throw new Error(
      `${this.constructor.name} does not support synchronous embed(). ` +
        'Use the async path (Router.route / embedAsync) or inject a provider ' +
        'whose supportsSync is true.',
    );
  }

  /**
   * Generate embeddings for multiple texts synchronously.
   *
   * Default loops `embed()`; both will throw unless the provider opted in
   * to sync support.
   */
  embedBatch(texts: string[]): number[][] {
    return texts.map((t) => this.embed(t));
  }

  /**
   * Whether this provider supports the synchronous `embed()` path.
   *
   * Defaults to false -- the async path is the contract every provider
   * is required to fulfil. Providers override this getter to return true
   * when they implement `embed()`.
   */
  get supportsSync(): boolean {
    return false;
  }

  /** Dimensionality of the embedding vectors. */
  abstract get dimension(): number;

  /** Name/identifier of the embedding model. */
  abstract get modelName(): string;
}
