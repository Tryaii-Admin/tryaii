/**
 * User priority system for model selection.
 *
 * Priorities let users express what matters to them (quality, cost, speed)
 * on a 1-5 scale. These get transformed into weights that influence scoring.
 */

export interface PrioritiesData {
  quality: number;
  cost: number;
  speed: number;
}

export class Priorities {
  /**
   * Each value is on a 1-5 scale:
   *   1 = don't care about this dimension
   *   3 = balanced (default)
   *   5 = this is critical
   */
  readonly quality: number;
  readonly cost: number;
  readonly speed: number;

  constructor(quality = 3, cost = 3, speed = 3) {
    this.quality = Math.max(1, Math.min(5, Math.round(quality)));
    this.cost = Math.max(1, Math.min(5, Math.round(cost)));
    this.speed = Math.max(1, Math.min(5, Math.round(speed)));
  }

  /**
   * Quality weight: 0.3 (priority 1) .. 1.2 (priority 5).
   *
   * **Not used by the router any more.** Under `satisficing-v1` the quality
   * priority reaches the algorithm only through the band width `eps` (see
   * `qualityTolerance` in the engine). Kept for backward-compatible reporting;
   * it must not be reintroduced into the combine step.
   */
  get qualityWeight(): number {
    return 0.3 + ((this.quality - 1) / 4) * 0.9;
  }

  /**
   * Cost weight `wc = (cost - 1) / 4`: 0 at priority 1 (the cost term is OFF),
   * 1.0 at priority 5. Inside the quality band the engine ranks on
   * `(wc*U_c + ws*U_s) / (wc + ws)`, so this is a *share*, not a scale, and
   * `wc + ws == 0` is the strict-quality path.
   *
   * Deliberately NOT a soft weight (`0.05 + 0.95(k-1)/4`): measured as a no-op
   * at (3,3,3), it degrades price-noise stability at (1,5,1) and breaks the
   * exactness of both regression anchors.
   */
  get costWeight(): number {
    return ((this.cost - 1) / 4) * 1.0;
  }

  /** Speed weight `ws = (speed - 1) / 4`: 0 at priority 1 (OFF), 1.0 at 5. */
  get speedWeight(): number {
    return ((this.speed - 1) / 4) * 1.0;
  }

  toDict(): PrioritiesData {
    return { quality: this.quality, cost: this.cost, speed: this.speed };
  }

  static fromDict(d: Partial<PrioritiesData>): Priorities {
    return new Priorities(d.quality ?? 3, d.cost ?? 3, d.speed ?? 3);
  }

  /** Preset: maximize quality, ignore cost and speed. */
  static performance(): Priorities {
    return new Priorities(5, 1, 1);
  }

  /** Preset: minimize cost, moderate quality. */
  static budget(): Priorities {
    return new Priorities(2, 5, 3);
  }

  /** Preset: fastest response, moderate quality. */
  static fast(): Priorities {
    return new Priorities(2, 3, 5);
  }

  /** Preset: balanced across all dimensions. */
  static balanced(): Priorities {
    return new Priorities(3, 3, 3);
  }
}

export const DEFAULT_PRIORITIES = new Priorities(3, 3, 3);
