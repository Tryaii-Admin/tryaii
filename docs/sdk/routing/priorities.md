# Priorities

`Priorities` expresses how much you care about quality, cost, and speed — each on a 1–5 scale (1 = don't care, 3 = balanced, 5 = critical). Exported from the package root in both SDKs, along with `DEFAULT_PRIORITIES` (3/3/3).

```python
from tryaii import Priorities
Priorities(quality=5, cost=1, speed=2)
Priorities.performance()   # (5,1,1) max quality
Priorities.budget()        # (2,5,3) min cost
Priorities.fast()          # (2,3,5) fastest
Priorities.balanced()      # (3,3,3)
Priorities.from_dict({"quality": 5})   # missing fields default to 3
```

```ts
import { Priorities, DEFAULT_PRIORITIES } from 'tryaii';
new Priorities(5, 1, 2);            // positional: quality, cost, speed
Priorities.performance();           // same four presets as Python
Priorities.fromDict({ quality: 5 });
```

## Validation & rounding

- Non-numeric values → `TypeError` (Python) / coerced (Node).
- Values are rounded **half-up** (both SDKs deliberately match `Math.round`, not Python banker's rounding) and clamped to [1, 5] — out-of-range inputs never error.

## How priorities become weights

Each axis maps to a scoring weight; quality has a higher floor so it always retains influence:

| Weight | Formula | Effective range |
|---|---|---|
| quality | `0.3 + (quality/5) × 0.9` | 0.48 – 1.2 |
| cost | `0.1 + (cost/5) × 0.9` | 0.28 – 1.0 |
| speed | `0.1 + (speed/5) × 0.9` | 0.28 – 1.0 |

Final score per model: `(q·qW + c·cW + s·sW) / (qW + cW + sW)` — see [scoring](scoring.md).

## Where priorities apply

- `Router.route(...)` and the [clients](../client/README.md) — per call or as a client-level default.
- The [OpenRouter integration](../client/openrouter.md) accepts a plain dict `{"quality": 5, "cost": 2, "speed": 3}` instead of the class.
- **Not** in [budget routing](../budget/README.md) — `route_dataset_with_budget` accepts a `priorities` argument but ignores it (the objective is fixed: maximize quality under budget).

Note: `TryaiiDreConfig.strategy` (`"balanced" | "performance" | "cost" | "speed"`) looks related but is currently unused by the router — pass `Priorities` per route instead.
