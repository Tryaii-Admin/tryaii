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
   * Quality weight: 0.3 (priority 1) .. 1.2 (priority 5). Quality always keeps a
   * baseline influence so a prompt is never scored on cost/speed alone -- this
   * also guarantees the weight total is never zero (no divide-by-zero in the
   * scoring engine even when cost and speed are both fully suppressed).
   */
  get qualityWeight(): number {
    return 0.3 + ((this.quality - 1) / 4) * 0.9;
  }

  /**
   * Cost weight: 0 (priority 1) .. 1.0 (priority 5). Fully suppressible -- a
   * priority of 1 removes cost from the decision entirely, so e.g.
   * `Priorities(5, 1, 1)` is a true quality-only route (previously cost/speed
   * kept a 0.28 floor that let a cheaper model out-rank a higher-quality one).
   */
  get costWeight(): number {
    return ((this.cost - 1) / 4) * 1.0;
  }

  /** Speed weight: 0 (priority 1) .. 1.0 (priority 5). Fully suppressible, like cost. */
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
