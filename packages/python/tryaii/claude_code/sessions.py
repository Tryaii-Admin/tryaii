"""Cold cache restarts in Claude Code sessions, from the local logs.

A Claude Code session keeps its prompt (system prompt, tools, conversation)
in Anthropic's prompt cache. When the user steps away for longer than the
cache lifetime (5 minutes or 1 hour), or switches model, the next call finds
nothing to read and writes the whole context again: a **cold restart**. Its
extra cost is what that write cost above reading the same tokens.

The rule (pinned by ``shared/monitor/fixtures/claude-code/cold-restarts.json``;
the server applies the same rule to live events):

- only the **main thread** of one session counts, in time order; subagent
  calls are excluded (a subagent starts with an empty cache by design);
- the first main call of a session is never cold (there was no cache yet);
- a call is cold when the gap since the previous main call of the same
  session is more than ``GAP`` (5 minutes, the shortest cache lifetime) and
  it wrote at least ``max(MIN_WRITE_TOKENS, its cache read tokens)`` (a warm
  call reads far more than it writes);
- its lifetime (``ttl``) is the call's own cache-write split: ``1h`` when the
  1-hour part is the larger, ``5m`` when only the 5-minute part is set; a
  call without a split takes the last split seen in the session, else
  ``DEFAULT_TTL`` (``5m``, the API's default; the server's default for live
  main-thread calls is ``1h``, so the shared fixture passes its default
  explicitly);
- the reason: ``expired`` when the gap is at least the ttl, else
  ``model_switch`` when the model differs from the previous main call's,
  else ``other`` (for example a changed system prompt or tool set).

Cost (``restart_cost``): the write's 5-minute part x (1.25 x input - cache
read price) plus its 1-hour part x (2 x input - cache read price), at API
list prices: what the rewrite cost above reading the same tokens. A write
without a 5m/1h split is priced whole at the call's resolved ``ttl``.

Privacy: the session is the log file's name (``Call.session``), a grouping
key that never leaves this module's callers: no event, report, output or
upload carries it. Reports name sessions by ordinal only ("Session 3").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

GAP = timedelta(minutes=5)
MIN_WRITE_TOKENS = 1024
TTLS = {"5m": timedelta(minutes=5), "1h": timedelta(hours=1)}
DEFAULT_TTL = "5m"
REASONS = ("expired", "model_switch", "other")
CACHE_WRITE_5M_FACTOR = 1.25
CACHE_WRITE_1H_FACTOR = 2.0


@dataclass(frozen=True)
class ColdRestart:
    """One cold restart (metadata only; ``session`` stays internal)."""

    key: str
    session: str
    ts: object
    model: str
    previous_model: str
    gap_s: float
    ttl: str
    reason: str
    write_tokens: int
    read_tokens: int


def call_ttl(call) -> Optional[str]:
    """``1h`` / ``5m`` from the call's own cache-write split, None without one."""
    if not call.has_breakdown:
        return None
    w1 = call.tokens("cache_write_1h_tokens")
    w5 = call.tokens("cache_write_5m_tokens")
    if w1 > w5:
        return "1h"
    if w5 > 0:
        return "5m"
    return None


def main_threads(calls) -> dict:
    """Session -> its main-thread calls in time order. Calls without a
    session (built by hand) and subagent calls are left out."""
    out: dict = {}
    for c in calls:
        if c.subagent or not c.session:
            continue
        out.setdefault(c.session, []).append(c)
    for group in out.values():
        group.sort(key=lambda c: (c.ts, c.key))
    return out


def is_cold_write(call) -> bool:
    return call.cache_write_tokens >= max(MIN_WRITE_TOKENS, call.tokens("cache_read_input_tokens"))


def cold_restarts(calls) -> list:
    """Every cold restart among ``calls``, oldest first."""
    found = []
    for session, group in main_threads(calls).items():
        last_ttl = None
        for i, c in enumerate(group):
            own = call_ttl(c)
            if i > 0:
                prev = group[i - 1]
                gap = c.ts - prev.ts
                if gap > GAP and is_cold_write(c):
                    ttl = own or last_ttl or DEFAULT_TTL
                    if gap >= TTLS[ttl]:
                        reason = "expired"
                    elif c.model != prev.model:
                        reason = "model_switch"
                    else:
                        reason = "other"
                    found.append(ColdRestart(
                        key=c.key, session=session, ts=c.ts, model=c.model,
                        previous_model=prev.model, gap_s=gap.total_seconds(), ttl=ttl,
                        reason=reason, write_tokens=c.cache_write_tokens,
                        read_tokens=c.tokens("cache_read_input_tokens")))
            last_ttl = own or last_ttl
    found.sort(key=lambda r: (r.ts, r.key))
    return found


def mark(calls) -> list:
    """Set ``cache_cold`` on every call: True / False for a main-thread call
    of a known session, None for a subagent call or a call without a session.
    Returns the cold restarts."""
    restarts = cold_restarts(calls)
    cold = {r.key: r for r in restarts}
    for c in calls:
        if c.subagent or not c.session:
            c.cache_cold = None
            c.cold = None
        else:
            c.cold = cold.get(c.key)
            c.cache_cold = c.cold is not None
    return restarts


def restart_cost(call, price) -> Optional[float]:
    """USD the rewrite cost above reading the same tokens (unrounded), at the
    ``pricing.Price`` given; None when unpriced. A write without a split is
    priced at ``call.cold.ttl`` (all 1h when that is ``1h``, else all 5m)."""
    if price is None:
        return None
    inp = price.input
    if price.long_context and call.context_tokens > price.long_context[0]:
        inp = price.long_context[1]
    read = price.cache_read_for(inp)
    if call.has_breakdown:
        w1 = call.tokens("cache_write_1h_tokens")
    else:  # no split: the whole write at the resolved lifetime
        w1 = call.cache_write_tokens if getattr(call.cold, "ttl", None) == "1h" else 0
    w5 = max(call.cache_write_tokens - w1, 0)
    return (w5 * (CACHE_WRITE_5M_FACTOR * inp - read)
            + w1 * (CACHE_WRITE_1H_FACTOR * inp - read))


def session_labels(calls) -> dict:
    """Session -> ``Session N``: main-thread sessions numbered by their first
    call (N from 1), the only way a session is ever named in output."""
    firsts = sorted((group[0].ts, session) for session, group in main_threads(calls).items())
    return {session: f"Session {i}" for i, (_ts, session) in enumerate(firsts, 1)}
