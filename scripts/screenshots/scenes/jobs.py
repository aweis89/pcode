"""A background job watching CI while the conversation carries on."""

from scene import Call, launch

MODEL = "claude:claude-opus-5"
SIZE = (100, 30)

CI = (
    'for i in $(seq 1 40); do echo "checks: $i/40 done, 0 failing"; sleep 2; done; '
    'echo "all checks passed"'
)

TURNS = {
    "watch CI": [
        [Call("shell", command=CI, background=True, purpose="watching CI on the PR")],
        [
            "CI is running as background job j1. I'll get a note when it finishes, "
            "so keep going and I'll report back then."
        ],
    ],
    "changelog": [
        [Call("read_file", path="README.md")],
        ["Drafted a changelog entry for 0.3.1 while CI runs."],
    ],
}

STEPS = [
    ("type", "Push the fix and watch CI"),
    ("key", "Enter"),
    ("wait", "report back then"),
    ("type", "Meanwhile, draft a changelog entry"),
    ("key", "Enter"),
    ("wait", "while CI runs"),
    ("shot", "jobs", "background jobs"),
    ("type", "/jobs"),
    ("key", "Enter"),
    ("wait", "this session"),
    ("shot", "jobs-popup", "/jobs"),
]

if __name__ == "__main__":
    launch(TURNS, model=MODEL)
