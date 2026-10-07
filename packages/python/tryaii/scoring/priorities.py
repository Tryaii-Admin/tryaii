"""
User priority system for model selection.

Priorities let users express what matters to them (quality, cost, speed) on a
1-5 scale. Under ``satisficing-v1`` (see ``scoring/engine.py``) they are used in
two distinct ways:

* **cost and speed** become the in-band ranker weights ``wc = (cost - 1) / 4``
  and ``ws = (speed - 1) / 4`` -- exactly :attr:`Priorities.cost_weight` and
  :attr:`Priorities.speed_weight`. A priority of 1 switches its term off
  completely; when both are 1 there is no secondary term at all and routing is
  strict quality.
* **quality** is used *only* through the band width,
  ``eps = EPS_UNIT * ((cost - 1) + (speed - 1)) / quality``. It is not a weight
  in the score. :attr:`Priorities.quality_weight` survives for
  backward-compatible reporting and for the all-no-signal fallback path.

Values are clamped to 1..5 and rounded half-up, which the Node SDK's
``Math.round`` depends on for parity.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class Priorities:
    """
    User priorities for model selection.

    Each value is on a 1-5 scale:
        1 = don't care about this dimension
        3 = balanced (default)
        5 = this is critical

    Examples:
        Priorities(quality=5, cost=1, speed=1)  # Best quality, ignore cost/speed
        Priorities(quality=2, cost=5, speed=4)  # Budget-focused, prefer fast
        Priorities(quality=3, cost=3, speed=3)  # Balanced (default)
    """

    quality: int = 3
    cost: int = 3
    speed: int = 3

    def __post_init__(self):
        for field_name in ("quality", "cost", "speed"):
            value = getattr(self, field_name)
            if not isinstance(value, (int, float)):
                raise TypeError(f"{field_name} must be a number, got {type(value)}")
            # Round-half-up to match Node's Math.round (3.6 -> 4, 4.5 -> 5).
            # Python's built-in round() uses banker's rounding (4.5 -> 4), so
            # use floor(value + 0.5) to stay in parity with the Node SDK.
            rounded = math.floor(value + 0.5)
            clamped = max(1, min(5, rounded))
            object.__setattr__(self, field_name, clamped)

    @property
    def quality_weight(self) -> float:
        """Quality weight: 0.3 (priority 1) .. 1.2 (priority 5).

        **Not used by the satisficing ranker** -- quality enters the algorithm
        only through the band width ``eps`` (see ``engine.quality_tolerance``).
        Kept for backward-compatible reporting, and used by the all-no-signal
        fallback path, where it guarantees the weight total is never zero.
        """
        return 0.3 + ((self.quality - 1) / 4) * 0.9

    @property
    def cost_weight(self) -> float:
        """``wc``, the in-band cost weight: 0 (priority 1) .. 1.0 (priority 5).

        Fully suppressible -- a priority of 1 removes cost from the decision
        entirely, so e.g. ``Priorities(5, 1, 1)`` is a true quality-only route.
        Inside the band the two weights are renormalised (``sec`` is divided by
        ``wc + ws``), so ``sec`` means the same thing at (1,5,1), (3,3,3) and
        (1,1,5); only their *ratio* matters there.
        """
        return ((self.cost - 1) / 4) * 1.0

    @property
    def speed_weight(self) -> float:
        """``ws``, the in-band speed weight: 0 (priority 1) .. 1.0 (priority 5).

        Suppressible exactly like :attr:`cost_weight`.
        """
        return ((self.speed - 1) / 4) * 1.0

    def to_dict(self) -> dict[str, int]:
        return {"quality": self.quality, "cost": self.cost, "speed": self.speed}

    @classmethod
    def from_dict(cls, d: dict) -> Priorities:
        return cls(
            quality=d.get("quality", 3),
            cost=d.get("cost", 3),
            speed=d.get("speed", 3),
        )

    @classmethod
    def performance(cls) -> Priorities:
        """Preset: maximize quality, ignore cost and speed."""
        return cls(quality=5, cost=1, speed=1)

    @classmethod
    def budget(cls) -> Priorities:
        """Preset: minimize cost, moderate quality."""
        return cls(quality=2, cost=5, speed=3)

    @classmethod
    def fast(cls) -> Priorities:
        """Preset: fastest response, moderate quality."""
        return cls(quality=2, cost=3, speed=5)

    @classmethod
    def balanced(cls) -> Priorities:
        """Preset: balanced across all dimensions."""
        return cls(quality=3, cost=3, speed=3)


DEFAULT_PRIORITIES = Priorities(quality=3, cost=3, speed=3)
