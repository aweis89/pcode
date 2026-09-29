"""/tree after editing an earlier prompt, which forks the conversation."""

from scene import Call, launch

MODEL = "claude:claude-opus-5"
SIZE = (100, 30)

TURNS = {
    "How does total()": [
        [Call("read_file", path="acme/orders.py")],
        [
            "`total()` sums the item prices and subtracts `discount` as a flat amount, "
            "so a 25 means 25.00 off, not 25%."
        ],
    ],
    "Decimal": [
        [
            Call(
                "edit_file",
                path="acme/orders.py",
                old_text="from dataclasses import dataclass\n",
                new_text="from dataclasses import dataclass\nfrom decimal import Decimal\n",
            )
        ],
        ["Switched prices and discounts to `Decimal`, so totals no longer drift."],
    ],
    "round": [
        [
            Call(
                "edit_file",
                path="acme/orders.py",
                old_text="return subtotal - order.discount",
                new_text="return round(subtotal - order.discount, 2)",
            )
        ],
        ["Kept floats and rounded `total()` to two places."],
    ],
}

STEPS = [
    ("type", "How does total() handle discounts?"),
    ("key", "Enter"),
    ("wait", "not 25%"),
    ("type", "Store prices as Decimal instead of float"),
    ("key", "Enter"),
    ("wait", "no longer drift"),
    # Edit the second prompt: that forks a new branch from the first answer.
    ("type", "/tree"),
    ("key", "Enter"),
    ("wait", "Store prices as Decimal"),
    ("key", "Up"),
    ("key", "Enter"),
    ("wait", "❯ Store prices"),
    ("key", "C-u"),
    ("type", "Keep floats, but round totals to two places"),
    ("key", "Enter"),
    ("wait", "rounded total()"),
    ("type", "/tree"),
    ("key", "Enter"),
    ("wait", "Keep floats"),
    ("shot", "tree", "/tree"),
]

if __name__ == "__main__":
    launch(TURNS, model=MODEL)
