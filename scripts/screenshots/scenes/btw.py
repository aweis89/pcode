"""The /btw side-question popup alongside a conversation."""

from scene import Call, launch

MODEL = "claude:claude-opus-5-5"
SIZE = (100, 22)

FIX_OLD = "    return subtotal - order.discount\n"
FIX_NEW = "    return round(subtotal * (1 - order.discount / 100), 2)\n"

BTW_ANSWER = (
    "Keeping `float` matches the existing `Order.items: list[float]` signature and "
    "keeps callers and JSON serialization untouched.\n\n"
    "Switch to `Decimal` (or integer cents) if you plan to add:\n"
    "- **Tax or multi-currency rounding** where intermediate binary float drift compounds\n"
    "- **Ledger or invoicing exports** that require exact decimal arithmetic\n\n"
    "For a single subtotal and percentage discount, `round(..., 2)` at the boundary "
    "is enough without breaking the public dataclass."
)

TURNS = {
    "Fix the percentage discount": [
        [Call("read_file", path="acme/orders.py")],
        [Call("edit_file", path="acme/orders.py", old_text=FIX_OLD, new_text=FIX_NEW)],
        [
            "Fixed `total()` in `acme/orders.py`: `discount` is now applied as a "
            "percentage and rounded to two decimal places."
        ],
    ],
    "Decimal instead of float": [[BTW_ANSWER]],
}

STEPS = [
    ("type", "Fix the percentage discount bug in total()."),
    ("key", "Enter"),
    ("wait", "rounded to two decimal places"),
    ("type", "/btw Why not use Decimal instead of float here?"),
    ("key", "Enter"),
    ("wait", "Side questions"),
    ("wait", "public dataclass."),
    ("shot", "btw", "/btw"),
]

if __name__ == "__main__":
    launch(TURNS, model=MODEL)
