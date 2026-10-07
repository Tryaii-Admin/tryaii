export {
  ScoringEngine,
  EPS_UNIT,
  PRICE_LO_PER_M,
  PRICE_HI_PER_M,
  T300_LO_S,
  T300_HI_S,
  FALLBACK_MEDIAN_TTFT_MS,
  FALLBACK_P25_SPEED_UTILITY,
  SIGNAL_FLAGS,
  IMPUTED_TERM_WEIGHT,
  COVERAGE_EXPONENT,
  computeBenchmarkCoverage,
  avgPricePer1k,
  costUtility,
  t300Seconds,
  speedUtility,
  speedUtilityFromT300,
  registrySpeedStats,
  qualityTolerance,
  satisficingCombine,
} from './engine.js';
export type { ModelScore, RegistrySpeedStats, SpeedUtility, CombinedScore } from './engine.js';
export { Priorities, DEFAULT_PRIORITIES } from './priorities.js';
export type { PrioritiesData } from './priorities.js';
export { BenchmarkNormalizer, NormalizationRange, NORMALIZATION_RANGES } from './benchmarks.js';
