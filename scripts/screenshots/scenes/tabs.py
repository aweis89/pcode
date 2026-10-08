"""Five pcode sessions in iTerm2 tabs, each tab named for its session and
showing how far along it is: plans at different stages, one failed turn and
one finished.

Not a single pane like the other scenes: `run.py` starts every tab in `TABS`
as its own session (this file with the tab's name as its argument), plays each
tab's steps side by side, then draws `ACTIVE`'s screen under an iTerm2 tab bar
built from all of them. `run.py --live tabs --tab NAME` plays one tab;
`iterm_window.py tabs` opens them all as real tabs in a new iTerm2 window.
"""

import sys
from dataclasses import dataclass, field

from pydantic_ai.exceptions import ModelHTTPError
from scene import Call, Fail, launch

MODEL = "claude:claude-opus-5-5"
SIZE = (110, 40)
ACTIVE = "discount"
# Holds a running turn on its current step until the scene ends.
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


@dataclass
class Tab:
    """One session: a short first turn that gets it named `title`, then `work`.

    `work` is the second turn's responses; `wait` is text on screen once the
    turn is where the shot wants it. `workers` holds the turns of sub-agents it
    delegates to, keyed by a phrase from their task that the prompts don't hold.
    """

    title: str
    ask: str
    answer: str
    task: str
    work: list[list]
    wait: str
    workers: dict = field(default_factory=dict)
    turns: dict = field(init=False)

    def __post_init__(self):
        self.turns = {
            # pcode names a session once its first turn ends, by asking the same
            # scripted model; that request quotes the first prompt, so its key
            # comes first.
            "First message from the user": [[self.title]],
            self.ask: [[self.answer]],
        }
        if self.task:
            self.turns[self.task] = self.work
        self.turns.update(self.workers)

    @property
    def steps(self) -> list[tuple]:
        steps = [
            ("type", self.ask),
            ("key", "Enter"),
            ("wait", self.answer[-30:]),
            ("title", self.title),
        ]
        if self.task:
            steps += [("type", self.task), ("key", "Enter")]
        return steps + [("wait", self.wait)]


DISCOUNT = [
    "Find where the discount is applied",
    "Apply the discount as a percentage",
    "Add a regression test for percentage discounts",
    "Get an independent review of the fix",
    "Commit the fix",
]
REVIEWER = [
    "Read the diff against main",
    "Check edge cases: 0%, 100%, fractional",
    "Report findings",
]
REVIEW = (
    "Review the discount fix in acme/orders.py: total() now treats discount as "
    "a percentage. Check the diff against main and edge cases (0%, 100%, "
    "fractional) and report anything wrong. Do not edit files."
)
EDGES = (
    "python3 -c 'from acme.orders import Order, total; "
    "print([total(Order(1, [19.99, 5.01], discount=d)) for d in (0, 12.5, 100)])'" + HOLD
)
TEST_OLD = "    assert total(Order(1, [10.0, 30.0], discount=25)) == 30.0\n"
TEST_NEW = TEST_OLD + (
    "\n\ndef test_zero_discount_keeps_subtotal():\n    assert total(Order(2, [12.5])) == 12.5\n"
)
CSV = [
    "Read the Order model and its fields",
    "Add an orders_to_csv() exporter",
    "Wire it to GET /orders.csv",
    "Test quoting and empty orders",
    "Document the endpoint",
]
PYTHON = [
    "Bump requires-python to 3.14",
    "Replace deprecated datetime.utcnow() calls",
    "Run the tests on 3.14",
    "Fix the new typing warnings",
    "Update the CI matrix",
]
FIX_OLD = "    return subtotal - order.discount\n"
FIX_NEW = "    return subtotal * (1 - order.discount / 100)\n"

TABS = {
    "discount": Tab(
        title="Fix percentage discounts",
        ask="Orders with a 25% discount come out wrong. Where is the discount applied?",
        answer="In total() in acme/orders.py, which subtracts the discount as a flat amount.",
        task="Fix it, with a test.",
        work=[
            [plan(DISCOUNT, 0)],
            [Call("read_file", path="acme/orders.py")],
            [plan(DISCOUNT, 1)],
            [Call("edit_file", path="acme/orders.py", old_text=FIX_OLD, new_text=FIX_NEW)],
            [plan(DISCOUNT, 2)],
            [Call("edit_file", path="tests/test_orders.py", old_text=TEST_OLD, new_text=TEST_NEW)],
            [
                "Fixed and tested. Handing the change to a worker for an independent "
                "review of the edge cases before committing.",
                plan(DISCOUNT, 3),
                Call(
                    "delegate_task",
                    agent_name="worker",
                    task=REVIEW,
                    purpose="reviewing the discount fix",
                ),
            ],
        ],
        wait="checking discount edge cases",
        # The worker runs on the same scripted model; its prompt is its task.
        workers={
            "Review the discount fix": [
                [plan(REVIEWER, 0)],
                [Call("shell", command="git diff main -- acme/orders.py")],
                [plan(REVIEWER, 1)],
                [Call("shell", command=EDGES, purpose="checking discount edge cases")],
            ]
        },
    ),
    "csv": Tab(
        title="Add CSV export",
        ask="Do we have any way to export orders today?",
        answer="No. Orders only exist as Order objects in acme/orders.py.",
        task="Add a CSV export endpoint for orders.",
        work=[
            [plan(CSV, 1)],
            [
                Call(
                    "shell",
                    command="grep -n 'class Order' -A4 acme/orders.py" + HOLD,
                    purpose="reading the Order model",
                )
            ],
        ],
        wait="reading the Order model",
    ),
    "python": Tab(
        title="Upgrade to Python 3.14",
        ask="What Python version does acme-api target?",
        answer="pyproject.toml doesn't pin one yet: there's no requires-python.",
        task="Move us to Python 3.14 and fix whatever breaks.",
        work=[
            [plan(PYTHON, 2)],
            [
                Call(
                    "shell",
                    command="python3 -m unittest discover tests" + HOLD,
                    purpose="running the tests on 3.14",
                )
            ],
        ],
        wait="running the tests on 3.14",
    ),
    "flaky": Tab(
        title="Debug flaky total test",
        ask="test_total_applies_discount failed once in CI. Is it flaky?",
        answer="It compares floats with ==, so it can be off by a rounding error.",
        task="Find out why and fix it.",
        work=[
            [
                Fail(
                    ModelHTTPError(
                        status_code=500,
                        model_name="claude-opus-5-5",
                        body={
                            "type": "error",
                            "error": {"type": "api_error", "message": "Internal server error"},
                        },
                    )
                )
            ]
        ],
        wait="Internal server error",
    ),
    "explain": Tab(
        title="How order totals work",
        ask="Explain how an order's total is calculated.",
        answer="total() sums the item prices, then subtracts order.discount from the subtotal.",
        task="",
        work=[],
        wait="from the subtotal",
    ),
}

if __name__ == "__main__":
    launch(TABS[sys.argv[1]].turns, model=MODEL)
