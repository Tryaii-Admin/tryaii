"""
User priority system for model selection.

Priorities let users express what matters to them (quality, cost, speed)
on a 1-5 scale. These get transformed into weights that influence scoring.
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

        Quality always keeps a baseline influence so a prompt is never scored on
        cost/speed alone -- this also guarantees the weight total is never zero
        (no divide-by-zero in scoring even when cost and speed are both fully
        suppressed).
        """
        return 0.3 + ((self.quality - 1) / 4) * 0.9

    @property
    def cost_weight(self) -> float:
        """Cost weight: 0 (priority 1) .. 1.0 (priority 5).

        Fully suppressible -- a priority of 1 removes cost from the decision
        entirely, so e.g. ``Priorities(5, 1, 1)`` is a true quality-only route
        (previously cost/speed kept a 0.28 floor that let a cheaper model
        out-rank a higher-quality one).
        """
        return ((self.cost - 1) / 4) * 1.0

    @property
    def speed_weight(self) -> float:
        """Speed weight: 0 (priority 1) .. 1.0 (priority 5). Suppressible, like cost."""
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
