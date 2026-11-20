"""The /diffs viewer with a two-file edit and an inline review note."""

from scene import Call, launch

MODEL = "claude:claude-opus-5-5"
SIZE = (114, 28)

FIX_OLD = "    return subtotal - order.discount\n"
FIX_NEW = "    return round(subtotal * (1 - order.discount / 100), 2)\n"

TEST_OLD = "    assert total(Order(1, [10.0, 30.0], discount=25)) == 30.0\n"
TEST_NEW = (
    TEST_OLD
    + "\n\n"
    + "def test_zero_discount_keeps_subtotal():\n"
    + "    assert total(Order(2, [12.5, 7.5])) == 20.0\n"
)

TURNS = {
    "discount": [
        [Call("read_file", path="acme/orders.py")],
        [
            Call("edit_file", path="acme/orders.py", old_text=FIX_OLD, new_text=FIX_NEW),
            Call("edit_file", path="tests/test_orders.py", old_text=TEST_OLD, new_text=TEST_NEW),
        ],
        [
            "Fixed `total()` in `acme/orders.py` to apply `discount` as a percentage "
            "rounded to cents, and added a zero-discount test in `tests/test_orders.py`."
        ],
    ],
}

STEPS = [
    ("type", "Fix the percentage discount bug in total() and add a test for 0%."),
    ("key", "Enter"),
    ("wait", "zero-discount test"),
    ("type", "/diffs"),
    ("key", "Enter"),
    ("wait", "All changes (2)"),
    # Tab moves focus from the search bar into the diff pane.
    ("key", "Tab"),
    # Move down onto the added return line in acme/orders.py (row 5).
    ("key", "Down"),
    ("key", "Down"),
    ("key", "Down"),
    ("key", "Down"),
    ("key", "Down"),
    ("key", "C-n"),
    ("wait", "Note on"),
    ("type", "Also guard against negative discounts"),
    ("key", "Enter"),
    ("wait", "✎ Also guard against negative discounts"),
    ("shot", "diffs", "/diffs"),
]

if __name__ == "__main__":
    launch(TURNS, model=MODEL)
