# Priorities

`Priorities` expresses how much you care about quality, cost, and speed — each on a 1–5 scale (1 = minimum influence, 3 = balanced, 5 = critical). Under [`satisficing-v1`](scoring.md) the three axes do *different kinds* of work: cost and speed are the weights of the in-band ranker, while quality sets the width of the quality band and nothing else. The class is exported from the package root in both SDKs, along with `DEFAULT_PRIORITIES` (3/3/3).

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

## How priorities drive the decision

**Cost and speed become the in-band ranker's weights.** Inside the quality band, a model is ranked on `sec = (wc·U_c + ws·U_s) / (wc + ws)` — a weighted *average*, so only the ratio of the two matters:

| Weight | Formula | Effective range |
|---|---|---|
| cost (`wc`) | `(cost − 1)/4` | 0 – 1.0 |
| speed (`ws`) | `(speed − 1)/4` | 0 – 1.0 |

**Quality enters only through the band width.** It is not a weight in the score:

```
eps = 0.102 × ((cost − 1) + (speed − 1)) / quality        # in q' units
```

`eps` is how much quality the router may give up to get something cheaper or faster. It is linear in `(cost−1)+(speed−1)` and inversely proportional to the quality priority, and it is **0 exactly when cost and speed are both 1** — the same condition that switches the secondary term off, which is what makes `Priorities.performance()` (`5/1/1`) a true quality-only route. Priority 3/3/3 gives `wc = ws = 0.5` and `eps = 0.136`.

`quality_weight` / `qualityWeight` (`0.3 + 0.9 × (quality − 1)/4`, range 0.3–1.2) is still exported, but the ranker does **not** use it: it survives for backward-compatible reporting and for the all-no-signal fallback path. `scoring/priorities.py` / `scoring/priorities.ts` are the source of truth.

Final score per model: `q'` at `5/1/1`, `0.5 + 0.5·sec` in band, `0.5·q'` out of band — see [scoring](scoring.md).

## Where priorities apply

- `Router.route(...)` and the [clients](../client/README.md) — per call or as a client-level default.
- The [OpenRouter integration](../client/openrouter.md) accepts a plain dict `{"quality": 5, "cost": 2, "speed": 3}` instead of the class.
- **Not** in [budget routing](../budget/README.md) — `route_dataset_with_budget` accepts a `priorities` argument but ignores it (the objective is fixed: maximize quality under budget).

Note: `TryaiiDreConfig.strategy` (`"balanced" | "performance" | "cost" | "speed"`) looks related but is currently unused by the router — pass `Priorities` per route instead.
