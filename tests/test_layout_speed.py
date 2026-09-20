"""The closed-form split division must match prompt_toolkit's cell-by-cell one exactly."""

import random

import pytest
from prompt_toolkit.layout.containers import HSplit, VSplit, Window, WritePosition
from prompt_toolkit.layout.dimension import Dimension

from pcode.layout_speed import (
    _original_divide_heights,
    _original_divide_widths,
    divide,
    install_fast_layout_division,
)


def random_dimension(rng, weight):
    minimum = rng.randint(0, 6)
    preferred = rng.randint(minimum, minimum + 40)
    maximum = rng.choice([preferred, preferred + rng.randint(0, 60), 10**10])
    return Dimension(min=minimum, max=maximum, preferred=preferred, weight=weight)


def reference(dimensions, available):
    """prompt_toolkit's own algorithm on these dimensions (a split adds zero-width padding)."""
    split = VSplit([Window(width=d) for d in dimensions], padding=0)
    result = _original_divide_widths(split, available)
    return result if result is None else result[::2]


@pytest.mark.parametrize("seed", range(300))
def test_equal_weight_division_matches_prompt_toolkit(seed):
    rng = random.Random(seed)
    count = rng.randint(1, 7)
    weight = rng.choice([1, 1, 1, 3])
    dimensions = [random_dimension(rng, weight) for _ in range(count)]
    available = rng.randint(0, 160)
    assert divide(dimensions, available) == reference(dimensions, available)


def test_zero_weight_children_stay_at_minimum_while_others_cycle():
    dimensions = [
        Dimension.exact(1),
        Dimension(min=0, max=10**10, preferred=0, weight=0),
        Dimension(min=0, max=10**10, preferred=0),
        Dimension.exact(2),
    ]
    assert divide(dimensions, 40) == reference(dimensions, 40) == [1, 0, 37, 2]


def test_unequal_weights_and_too_little_space_defer_to_the_original():
    dimensions = [Dimension(min=1, weight=1), Dimension(min=1, weight=2)]
    assert divide(dimensions, 30) is None
    assert divide([Dimension.exact(5), Dimension.exact(5)], 4) is None


def test_installed_splits_agree_with_the_original_on_a_real_layout():
    install_fast_layout_division()
    rng = random.Random(7)
    for _ in range(50):
        windows = [
            Window(width=random_dimension(rng, 1), height=random_dimension(rng, 1))
            for _ in range(rng.randint(1, 5))
        ]
        width, height = rng.randint(1, 120), rng.randint(1, 50)
        vsplit, hsplit = VSplit(windows), HSplit(windows)
        assert vsplit._divide_widths(width) == _original_divide_widths(vsplit, width)
        position = WritePosition(0, 0, width, height)
        assert hsplit._divide_heights(position) == _original_divide_heights(hsplit, position)


def test_fast_division_is_not_per_cell():
    dimensions = [Dimension.exact(1), Dimension(min=0, max=10**10, preferred=0), Dimension.exact(1)]
    sizes = divide(dimensions, 100_000)
    assert sizes == [1, 99_998, 1]
