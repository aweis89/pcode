"""The /agents dashboard following two parallel workers mid-turn."""

from scene import Call, launch

MODEL = "claude:claude-opus-5-5"
SIZE = (116, 22)
HOLD = "; sleep 600"


def plan(steps: list[str], done: int) -> Call:
    """The whole plan with `done` steps completed and the next one in progress."""

    def status(index):
        return "completed" if index < done else "in_progress" if index == done else "pending"

    items = [
        {"id": f"s{index}", "content": text, "status": status(index)}
        for index, text in enumerate(steps)
    ]
    return Call("write_plan", items=items)


FIX_OLD = "    return subtotal - order.discount\n"
FIX_NEW = "    return round(subtotal * (1 - order.discount / 100), 2)\n"

EDGE_STEPS = [
    "Read the diff against main",
    "Check edge cases: 0%, 100%, fractional",
    "Report findings",
]
TEST_STEPS = [
    "Read tests/test_orders.py",
    "Run the existing test suite",
    "List missing test cases",
]

EDGE_TASK = (
    "Review the discount fix in acme/orders.py for edge cases (0%, 100%, "
    "negative, fractional). Report any issues; do not edit files."
)
TEST_TASK = (
    "Inspect tests/test_orders.py and run the test suite to see what discount "
    "coverage is missing. Do not edit files."
)

EDGES_CMD = (
    "python3 -c 'from acme.orders import Order, total; "
    "print([total(Order(1, [19.99, 5.01], discount=d)) for d in (0, 12.5, 100)])'" + HOLD
)
SUITE_CMD = "python3 -m unittest discover tests" + HOLD

TURNS = {
    "Fix the percentage discount": [
        [Call("read_file", path="acme/orders.py")],
        [Call("edit_file", path="acme/orders.py", old_text=FIX_OLD, new_text=FIX_NEW)],
        [
            "Applied the fix. Sending two workers in parallel to check edge cases "
            "and test coverage before we wrap up.",
            Call(
                "delegate_task",
                agent_name="worker",
                task=EDGE_TASK,
                purpose="reviewing edge cases",
                plan_step="edge-cases",
                model="claude:claude-sonnet-4-6",
            ),
            Call(
                "delegate_task",
                agent_name="worker",
                task=TEST_TASK,
                purpose="checking test coverage",
                plan_step="test-coverage",
                model="claude:claude-sonnet-4-6",
            ),
        ],
    ],
    "Review the discount fix": [
        [plan(EDGE_STEPS, 0)],
        [Call("shell", command="git diff main -- acme/orders.py")],
        [plan(EDGE_STEPS, 1)],
        [Call("shell", command=EDGES_CMD, purpose="checking discount edge cases")],
    ],
    "Inspect tests/test_orders.py": [
        [plan(TEST_STEPS, 0)],
        [Call("read_file", path="tests/test_orders.py")],
        [plan(TEST_STEPS, 1)],
        [Call("shell", command=SUITE_CMD, purpose="running the test suite")],
    ],
}

STEPS = [
    ("type", "Fix the percentage discount bug in total(), then check edge cases and tests."),
    ("key", "Enter"),
    ("wait", "2 jobs"),
    ("sleep", 0.5),
    ("type", "/agents"),
    ("key", "Enter"),
    ("wait", "2 running"),
    ("wait", "Tasks 1/3"),
    ("shot", "agents", "/agents"),
]

if __name__ == "__main__":
    launch(TURNS, model=MODEL)
