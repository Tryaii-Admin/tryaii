/**
 * tryaii monitor — the LLM-call wrapper's pure core (shared/monitor/SPEC.md).
 *
 * Slice 1 pure API (clock-free, routing-free, fixture-frozen): text
 * extraction, provider class / reachability / winner selection, pins, config
 * precedence, status aggregation and the local store. The openai wrapper and
 * the daemon transport are built on top of these. Mirrors monitor/__init__.py.
 *
 * Slice 2 adds the `managed` mode, policy-aware reachability, the remote
 * config long poll (`./remote.js`) and the background uploader
 * (`./upload.js`) -- shared/monitor/API.md.
 */

/**
 * Version of the data notice the developer accepted by setting
 * `TRYAII_API_KEY` (sent as `notice_version` with every upload batch;
 * docs/sdk/monitor.md "What is uploaded").
 */
export { NOTICE_VERSION } from './upload.js';

export {
  CONFIG_SCHEMA,
  DEFAULT_APP,
  DEFAULT_PROCESS,
  ENV_APP,
  ENV_MODE,
  MSG_BAD_MODE,
  MSG_MANAGED_NEEDS_MODEL,
  REMOTE_SCHEMA,
  callSiteProcess,
  emptyConfig,
  envFromOs,
  resolveApp,
  resolveMode,
  resolveProcess,
  sanitizeApp,
  setProcessMode,
  validMode,
  type AppResolution,
  type ConfigDoc,
  type ModeResolution,
  type ProcessConfig,
  type ProcessResolution,
} from './config.js';
export {
  CACHE_PROVIDERS,
  CACHE_TTLS,
  COST_DECIMALS,
  EVENT_SCHEMA,
  ROUTE_STATUSES,
  TAXONOMY_VERSION_KEY,
  USAGE_CACHE_KEYS,
  USAGE_TOKEN_KEYS,
  VECTOR_SCHEMA,
  buildEvent,
  buildVector,
  classificationFromWire,
  estimateCost,
  formatTs,
  newEventId,
  normalizeCacheTtlPolicy,
  normalizeCacheUsage,
  normalizeCacheWrites,
  normalizeUsage,
  parseTs,
  roundCost,
  taxonomyVersion,
  usageFromResponse,
  type CacheProvider,
  type CacheTtl,
  type CacheUsage,
  type CacheWrite,
  type CallSiteRecord,
  type Classification,
  type ErrorRecord,
  type EventFields,
  type RouteStatus,
  type Usage,
} from './events.js';
export { messageText, promptHash, routingText } from './extract.js';
export {
  buildReport,
  CacheTracker,
  cachedTokensFromUsage,
  lintPrompt,
  maskExcerpt,
  outcomeFromExpected,
  recommendationFrom,
  sectionKind,
  type CacheBlock,
  type CacheFinding,
  type CacheReport,
} from './cache.js';
export {
  MIN_EVENTS_SHORT,
  PINS_SCHEMA,
  PIN_TTL_MS,
  emptyPins,
  evaluatePin,
  makePin,
  pinFromFirstCall,
  pinValid,
  setPin,
  winnerCounts,
  type Pin,
  type PinBasis,
  type PinDecision,
  type PinsDoc,
} from './pins.js';
export {
  STATUS_SCHEMA,
  WINDOWS,
  aggregate,
  displayPath,
  sourceInfo,
  uploadInfo,
  windowSeconds,
} from './status.js';
export * as store from './store.js';
export {
  MODES,
  MODE_DYNAMIC,
  MODE_MANAGED,
  MODE_OFF,
  MODE_STABLE,
  PROVIDERS,
  PROVIDER_OPENAI,
  PROVIDER_OPENROUTER,
  PROVIDER_OTHER,
  STATUS_UNREACHABLE_MANAGED,
  SWAP_QUALITY_EPSILON,
  allowedModels,
  blendedPrice,
  filterRanking,
  managedTarget,
  nativeModelId,
  pickWinner,
  policyAllowed,
  providerClass,
  reachable,
  resolveAnchor,
  routeAllowed,
  routedModel,
  scoreId,
  scoreQuality,
  sendable,
  shadowAllowed,
  type Allowed,
  type ManagedDecision,
  type Mode,
  type Provider,
  type ScoreLike,
  type WinnerPick,
} from './winner.js';

// The openai wrapper, the sub-process context and the daemon transport
// (slice 1 runtime API; mirrors monitor/__init__.py's wrap / install / process).
export { currentProcess, process, runProcess } from './context.js';
export {
  DEFAULT_ROUTE_TIMEOUT_MS,
  ENV_ROUTE_TIMEOUT_MS,
  QUEUE_MAX,
  STATUS_DISABLED,
  STATUS_OK,
  STATUS_QUEUE_FULL,
  STATUS_SKIPPED,
  STATUS_TIMEOUT,
  STATUS_UNAVAILABLE,
  TOP_K,
  Transport,
  getTransport,
  inPathTimeoutMs,
  outcomeOk,
  type RouteFn,
  type RouteFnResult,
  type RouteOutcome,
  type TransportOptions,
} from './transport.js';
export {
  ENV_VECTORS,
  Monitor,
  MonitoredStream,
  PER_CALL_PROCESS_KEY,
  clock,
  defaults,
  errorRecord,
  install,
  vectorsEnabled,
  wrap,
  type CallPlan,
  type InstallOptions,
  type MonitorOptions,
  type WrapOptions,
} from './wrapOpenai.js';

// Slice 2 services: the background uploader and the dashboard long poll
// (mirrors tryaii.monitor.upload.Uploader / tryaii.monitor.remote.RemotePoller).
export { Uploader, type UploaderOptions, type UploadOutcome } from './upload.js';
export { RemotePoller, type PollOutcome, type RemotePollerOptions } from './remote.js';
export { type TransportPolicy } from './transport.js';
