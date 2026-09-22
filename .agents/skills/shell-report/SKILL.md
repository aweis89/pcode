---
name: shell-report
description: Analyze how the model used the shell/job tools in pcode's saved sessions. Use when asked whether the model polled, backgrounded, or misread a command, when judging a change to the shell tool's docstring or job notices, or when a session's shell usage looks off.
---

# Shell report

The transcript keeps only a display projection of shell results (large ones are
replaced with an "output omitted" line), so the step store is the source of
truth for what the model saw. `scripts/shell_report.py` reads it.

Run from the repository root.

```sh
make shell-report                                  # latest session
uv run python scripts/shell_report.py <id> -v      # every call, one row each
uv run python scripts/shell_report.py all -n 10    # recent sessions
```

Default output is counts only. `-v` prints each call's job id, tool, elapsed,
status and a clipped, redacted command line. Do not paste raw step-store
content into a report: tool results hold file contents and command output.

## Reading the result

- `[warn] ... sleep between turns` -- the model polled instead of using
  `wait_for_job` or letting the exit notice arrive. The tool docstring in
  `src/pcode/shell_tools.py` is the lever.
- `[warn] ... exit 0 with failure text` -- `make test | tail` masks the
  status. Expected under `/bin/sh -c`; the point is to know the UI's ✓ and the
  job notice's `exit 0` were wrong for those calls, not to switch on pipefail
  (which turns `big | head` into `exit 141`).
- `handles returned` vs `wait_for_job` vs `notices` -- a healthy long session
  backgrounds a long run, does other work, and either waits or gets the notice.
  Many handles with no waits means foreground commands kept hitting the
  270 s ceiling.
- `cd into the workspace` / `read-only command(s)` -- token waste, not errors.
  The UI already strips the redundant `cd` from previews.

## Verify a change

```sh
uv run pytest -q tests/test_shell_report.py tests/test_jobs.py
uv run ruff check .
```

A docstring or notice change is only verified once a *new* session recorded
with it reads better here; the tests prove the report, not the model.
