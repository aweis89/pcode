"""A turn partway through its plan: the Tasks widget above the editor."""

from scene import Call, launch

MODEL = "claude:claude-opus-5-5"
SIZE = (100, 30)

STEPS_TEXT = [
    ("read", "Find where the discount is applied"),
    ("fix", "Apply discount as a percentage"),
    ("test", "Add a regression test for percentage discounts"),
    ("run", "Run the test suite"),
    ("docs", "Note the fix in the README"),
]


def plan(done: int):
    """The whole plan with `done` steps completed and the next one in progress."""

    def status(index):
        return "completed" if index < done else "in_progress" if index == done else "pending"

    items = [
        {"id": key, "content": text, "status": status(index)}
        for index, (key, text) in enumerate(STEPS_TEXT)
    ]
    return Call("write_plan", items=items)


FIX_OLD = "    return subtotal - order.discount\n"
FIX_NEW = "    return subtotal * (1 - order.discount / 100)\n"
TEST_OLD = "    assert total(Order(1, [10.0, 30.0], discount=25)) == 30.0\n"
TEST_NEW = TEST_OLD + (
    "\n\ndef test_zero_discount_keeps_subtotal():\n    assert total(Order(2, [12.5])) == 12.5\n"
)
# Long enough to hold the turn on the running step while the shot is taken.
SUITE = "python3 -m unittest discover tests; sleep 25"

TURNS = {
    "discount": [
        [plan(0)],
        [Call("read_file", path="acme/orders.py")],
        [plan(1)],
        [Call("edit_file", path="acme/orders.py", old_text=FIX_OLD, new_text=FIX_NEW)],
        [plan(2)],
        [Call("edit_file", path="tests/test_orders.py", old_text=TEST_OLD, new_text=TEST_NEW)],
        [plan(3)],
        [Call("shell", command=SUITE, purpose="running the test suite")],
    ],
}

STEPS = [
    ("type", "Orders with a 25% discount come out wrong. Fix it, with a test."),
    ("key", "Enter"),
    ("wait", "running the test suite"),
    ("shot", "tasks", "tasks"),
]

if __name__ == "__main__":
    launch(TURNS, model=MODEL)
