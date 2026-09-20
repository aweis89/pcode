"""Faster width/height division for prompt_toolkit splits.

Every repaint (each spinner frame, each scrollback flush) re-lays out the whole
bottom block. prompt_toolkit's ``VSplit._divide_widths`` and
``HSplit._divide_heights`` grow the children one cell at a time through a
weighted round-robin generator, re-summing the sizes on every step, so one
split costs O(width × children) and the eight splits in pcode's layout were
~45% of a repaint. Every split here uses the default weight, and for equal
weights the round-robin is a plain cycle, which can be applied in whole
rounds. Unequal weights keep the original path.
"""

from prompt_toolkit.layout.containers import HSplit, VSplit, WritePosition
from prompt_toolkit.layout.dimension import Dimension, sum_layout_dimensions


def divide(dimensions: list[Dimension], available: int, *, fill: bool = True) -> list[int] | None:
    """Sizes for ``dimensions`` in ``available`` cells, exactly as prompt_toolkit divides them.

    ``fill`` False stops at the preferred sizes, as ``_divide_heights`` does once
    the application is done.
    """
    if not dimensions:
        return []
    weights = [d.weight for d in dimensions]
    positive = [w for w in weights if w > 0]
    if not positive or any(w != positive[0] for w in positive):
        return None  # Caller falls back to the original algorithm.
    total = sum_layout_dimensions(dimensions)
    if total.min > available:
        return None
    sizes = [d.min for d in dimensions]
    order = [i for i, w in enumerate(weights) if w > 0]
    position = 0  # Where the round-robin cycle continues between the two passes.

    def grow(limits: list[int], stop: int) -> None:
        nonlocal position
        remaining = stop - sum(sizes)
        while remaining > 0:
            open_items = [i for i in order if sizes[i] < limits[i]]
            if not open_items:
                return  # The original would spin forever here; it cannot happen.
            headroom = min(limits[i] - sizes[i] for i in open_items)
            rounds = min(headroom, remaining // len(open_items))
            if rounds:
                for i in open_items:
                    sizes[i] += rounds
                remaining -= rounds * len(open_items)
                if remaining == 0:
                    # The cycle stopped right after the last open item it
                    # visited, which is the last open one counting round
                    # from the current position.
                    cycle = order[position:] + order[:position]
                    last = max(cycle.index(i) for i in open_items)
                    position = (position + last + 1) % len(order)
                continue
            # Fewer cells left than open items: hand them out one by one from
            # the current position, skipping saturated items as the cycle does.
            while remaining > 0:
                i = order[position]
                position = (position + 1) % len(order)
                if sizes[i] < limits[i]:
                    sizes[i] += 1
                    remaining -= 1

    grow([d.preferred for d in dimensions], min(available, total.preferred))
    if fill:
        grow([d.max for d in dimensions], min(available, total.max))
    return sizes


_original_divide_widths = VSplit._divide_widths
_original_divide_heights = HSplit._divide_heights


def _divide_widths(self: VSplit, width: int) -> list[int] | None:
    children = self._all_children
    if not children:
        return []
    dimensions = [c.preferred_width(width) for c in children]
    sizes = divide(dimensions, width)
    if sizes is None and sum_layout_dimensions(dimensions).min <= width:
        return _original_divide_widths(self, width)
    return sizes


def _divide_heights(self: HSplit, write_position: WritePosition) -> list[int] | None:
    from prompt_toolkit.application.current import get_app

    if not self.children:
        return []
    width, height = write_position.width, write_position.height
    dimensions = [c.preferred_height(width, height) for c in self._all_children]
    sizes = divide(dimensions, height, fill=not get_app().is_done)
    if sizes is None and sum_layout_dimensions(dimensions).min <= height:
        return _original_divide_heights(self, write_position)
    return sizes


def install_fast_layout_division() -> None:
    """Replace the per-cell division on every split; idempotent."""
    VSplit._divide_widths = _divide_widths
    HSplit._divide_heights = _divide_heights
