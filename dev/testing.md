# Testing and debugging notes

## Tmux regressions (`make test-all`)

The tmux tests run in parallel and beside other worktrees' runs only because none of them races the clock, so a tmux test that fails under load has a timing dependency to fix, not a busy machine to wait out.

Two common traps:
- **Fixed `sleep` in scripted turns**: do not hold a scripted turn open with a fixed `sleep` for the test to act inside; use `await gate()` and `release()`.
- **Transcript notes after resize/split/kill-pane**: the debounced resize replay erases notes already on screen, so resize through `resize(pane, ...)` first before asserting on a transcript note.

## Socket transport (`make test-socket`)

`make test-socket` (part of `make test-all`) reruns the fast suite with every `PreviewApp` session moved into a `SessionHost` over a real socket (`tests/socket_transport.py`):
- The host's state (queue, prompt row, busy, side questions) reaches the terminal a moment after the runtime acts, so a test that sets an event inside a fake runtime and then asserts on `app.activity` must wait for the value instead.
- Keep your own handle on a fake runtime: `app.runtime` is the host's journal reader under that transport.
- Mark a test `@pytest.mark.in_process(reason)` only where hosted behavior is meant to differ (e.g. a hosted `/quit` detaches instead of cancelling).

## `tests/test_terminal.py` in worktrees

`tests/test_terminal.py` spawns real `pcode` with the real environment, so once a session worktree holds unmerged commits its Ctrl+D exit stops on `"Merge and remove the worktree?"` and the test times out. Uncommitted edits to tracked files skip the question, so it passes mid-change and starts failing the moment everything is committed. That failure is the worktree's state, not the change; confirm with the same test at `HEAD~1` in a throwaway worktree.

## Comparing against `HEAD` / `HEAD~1` in a throwaway worktree

- Add a throwaway worktree and run the test with `PYTHONPATH=<that>/src` (which wins over the editable install) before believing a regression.
- **Match the checkout path length**: console output is wrapped to a fixed width, so a shorter checkout path silently rewraps it and width-sensitive assertions (`test_mcp_enable`, anything matching a traceback line) flip. Name the worktree to match, or the comparison lies in both directions.

## Subprocess tests and `--no-worktree`

A test that launches `pcode` from the repo (a subprocess, or in-process `main()`) with `worktree` on checks out a real `.worktrees/pcode-*` and waits on its `uv sync`: slow startups, timeouts, and leaked worktrees. The committed `.pcode/preferences.json` keeps it off, but `conftest.py` still hides the overlay for in-process runs and subprocesses pass `--no-worktree` (the tmux `pane` fixture, `test_terminal.py`) so a local override can't trigger it.

## Artificial CPU load

When a test needs artificial CPU load, give each burner its own deadline:

```sh
python3 -c "import time; t=time.time()+60; exec('while time.time()<t: pass')" &
```

Never use a trailing `pkill` in the same command. A stopped job, a closed session, or a `pkill` that fails under that very load (`"Cannot get process list"`) leaves `while True` loops spinning at 100% per core until reboot; that happened here once with 16 of them.
