"""Plan step sizes and the progress they weight.

Kept free of Pydantic AI so the terminal's tab bar can import it at startup.
"""

from collections.abc import Mapping, Sequence
from typing import Literal

Size = Literal["S", "M", "L"]

# Relative effort, not time: models compare sizes far better than they guess
# durations, and three buckets leave nothing to agonise over. Unsized steps
# count as M. Roughly geometric, so one L outweighs a few quick steps.
SIZE_WEIGHTS: dict[str, int] = {"S": 1, "M": 3, "L": 8}


def plan_progress(items: Sequence[Mapping]) -> float | None:
    """How far through the plan the work is, 0..1, weighted by step size.

    Cancelled steps leave the total, so a dropped step never strands the bar
    short of full, and the running step counts half: the bar moves when work
    starts rather than only when it lands. None when nothing counts.
    """
    total = done = 0.0
    for item in items:
        status = item.get("status")
        if status == "cancelled":
            continue
        weight = SIZE_WEIGHTS.get(item.get("size") or "M", SIZE_WEIGHTS["M"])
        total += weight
        if status == "completed":
            done += weight
        elif status == "in_progress":
            done += weight / 2
    return done / total if total else None
