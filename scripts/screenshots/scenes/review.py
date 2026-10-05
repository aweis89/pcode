"""A fix shown as a delta word diff, then a worker reviewing it under the plan.

Run it with `--iterm` for your iTerm2 profile's colors, or `--live` to watch it
in your own terminal and take the screenshot yourself.
"""

from scene import Call, Think, launch

MODEL = "claude:claude-opus-5-5"
SIZE = (110, 44)


def plan(steps, done: int):
    """The whole plan with `done` steps completed and the next one in progress."""

    def status(index):
        return "completed" if index < done else "in_progress" if index == done else "pending"

    items = [
        {"id": key, "content": text, "status": status(index)}
        for index, (key, text) in enumerate(steps)
    ]
    return Call("write_plan", items=items)


PARENT = [
    ("fix", "Apply the discount as a percentage"),
    ("review", "Get an independent review of the fix"),
    ("commit", "Commit the fix"),
]
WORKER = [
    ("diff", "Read the diff against main"),
    ("edges", "Check edge cases: 0%, 100%, fractional"),
    ("report", "Report findings"),
]

# One changed line, so delta highlights the changed words within it.
FIX_OLD = "    subtotal = sum(order.items)\n    return subtotal - order.discount\n"
FIX_NEW = (
    "    subtotal = sum(order.items)\n    return round(subtotal * (1 - order.discount / 100), 2)\n"
)
REVIEW = (
    "Review the discount fix in acme/orders.py: total() now treats discount as "
    "a percentage. Check the diff against main and edge cases (0%, 100%, "
    "fractional) and report anything wrong. Do not edit files."
)
# Holds the worker on its edge-case step while the shot is taken; the runner
# stops it when the scene ends.
EDGES = (
    "python3 -c 'from acme.orders import Order, total; "
    "print([total(Order(1, [19.99, 5.01], discount=d)) for d in (0, 12.5, 100)])'; sleep 600"
)

TITLE = "Fix percentage discount in order totals"

TURNS = {
    # pcode names a session once its first turn ends, by asking the same
    # (scripted) model: that request quotes the first prompt, so its key comes
    # first, ahead of the keys the quoted prompt also contains.
    "First message from the user": [[TITLE]],
    "Where is the discount applied": [
        [
            Think(
                "A 25% discount coming out wrong smells like the discount being "
                "applied as a flat amount. Check total() before changing anything."
            ),
            Call("read_file", path="acme/orders.py"),
        ],
        [
            "In `total()` in acme/orders.py. It subtracts `order.discount` as a flat "
            "amount, so 25% off a $40 order takes off $25 and charges $15 instead "
            "of $30.",
        ],
    ],
    # Not "discount": the worker's task mentions it too.
    "get the fix reviewed": [
        [
            Think(
                "Scaling the subtotal fixes it; round to cents so float noise doesn't "
                "leak into totals. 100% should then give 0.0, worth having the "
                "reviewer check."
            ),
            "Making the discount a percentage of the subtotal, rounded to cents:",
            plan(PARENT, 0),
            Call("edit_file", path="acme/orders.py", old_text=FIX_OLD, new_text=FIX_NEW),
        ],
        [
            "That fixes the example from the report: $40 at 25% off is now $30.00. "
            "Before committing, I'm handing the change to a worker for an "
            "independent review of the edge cases.",
            plan(PARENT, 1),
            Call(
                "delegate_task",
                agent_name="worker",
                task=REVIEW,
                purpose="reviewing the discount fix",
            ),
        ],
    ],
    # The worker runs on the same scripted model; its prompt is the task text.
    "Review the discount fix": [
        [plan(WORKER, 0)],
        [Call("shell", command="git diff main -- acme/orders.py")],
        [plan(WORKER, 1)],
        [Call("shell", command=EDGES, purpose="checking discount edge cases")],
    ],
}

STEPS = [
    ("type", "Orders with a 25% discount come out wrong. Where is the discount applied?"),
    ("key", "Enter"),
    ("wait", "instead of $30"),
    ("title", TITLE),
    ("type", "Fix it and get the fix reviewed."),
    ("key", "Enter"),
    ("wait", "checking discount edge cases"),
    ("shot", "review"),
]

if __name__ == "__main__":
    launch(TURNS, model=MODEL)
