"""The /tools inspector after a turn with a failing and then passing check."""

from scene import Call, launch

MODEL = "claude:claude-opus-5"
SIZE = (110, 32)

CHECK = (
    "python3 -c 'from acme.orders import Order, total; "
    "got = total(Order(1, [10.0, 30.0], discount=25)); "
    'assert got == 30.0, f"total() returned {got}"; print("ok: 30.0")\''
)

TURNS = {
    "discount": [
        [Call("shell", command=CHECK, purpose="reproducing the discount bug")],
        [Call("read_file", path="acme/orders.py")],
        [
            Call(
                "edit_file",
                path="acme/orders.py",
                old_text="return subtotal - order.discount",
                new_text="return subtotal - subtotal * order.discount / 100",
            )
        ],
        [Call("shell", command=CHECK, purpose="checking the fix")],
        [Call("shell", command="git diff --stat")],
        ["Fixed: `discount` is a percentage, so the order now totals 30.00."],
    ],
}

STEPS = [
    ("type", "Orders with a 25% discount come out wrong. Can you fix it?"),
    ("key", "Enter"),
    ("wait", "now totals 30.00"),
    ("type", "/tools"),
    ("key", "Enter"),
    ("wait", "calls"),
    ("key", "Down"),  # From the newest call to the passing check.
    ("wait", "ok: 30.0"),
    ("shot", "tools", "/tools"),
    ("key", "C-x"),
    ("wait", "Status: Failed"),
    ("shot", "tools-failed", "/tools · failures"),
]

if __name__ == "__main__":
    launch(TURNS, model=MODEL)
