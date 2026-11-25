# Editor first-paint latency: investigation notes

These are ad hoc notes for whoever picks this up next. Goal: get the editor
on screen and accepting typing as early as possible on a new or resumed
session, even if tasks, metadata, and the agent runtime are still loading.

## Where it stands

- Agent runtime construction already runs off the event loop
  (`Controller.initialize_runtime` uses `asyncio.to_thread`), and the editor
  shows "Starting pcode" while it runs. Typed input is queued until it's ready.
- Fixed in 217712a6: `_stopping_provider_processes` (src/pcode/app.py)
  imported `pcode.antigravity` and `pcode.claude_sdk` (pydantic_ai plus the
  SDK) on the loop thread before the editor started. They are now looked up in
  `sys.modules` on exit instead. `tests/test_startup.py::test_provider_shutdown_wrapper_does_not_import_agent_stack`
  guards this.
- Measured time to the first editor frame (`❯` visible), 3 runs each:
  - in-process (`--no-host`): ~1.9-2.4s before, ~0.95s after
  - hosted (`--host`): ~2.0s before, ~1.05s after
- `import pcode.app` alone takes ~0.33s, so about 0.6s is still unaccounted
  for. Candidates seen in stack samples: the theme probe
  (`theme.detect_theme` / `_query_background`), `create_prompt` (drafts load,
  keyboard protocol setup), and the first render (toolbar →
  `current_effort`). These haven't been quantified yet.

## How to measure

Run pcode in a detached tmux pane and poll for the prompt glyph:

```sh
tmux new-session -d -s probe -x 120 -y 40 \
  "cd <checkout> && env -u PCODE_DOCK uv run --no-sync pcode --no-worktree --no-host; sleep 30"
# poll: tmux capture-pane -p -t probe | grep -q '❯'
```

To find where the time goes, sample the main thread's stack from a daemon
thread (`sys._current_frames()[main_thread_id]` every 20ms, written to a
file), then launch through `pcode.cli.main`. `-X importtime` only covers
imports, so it misses work done after the event loop starts.

## Traps hit along the way

- **`PCODE_DOCK=1` inherited from Pcode.app.** Every probe then waits the 3s
  dock handshake (`dock.CONNECT_TIMEOUT`) that tmux never answers, so first
  paint looks ~3s worse than it is. Always `env -u PCODE_DOCK`.
- **Never start a session host inside your own session's worktree.** If that
  worktree's tree is clean when the host exits, the host's cleanup removes
  it, and the session working there dies (`Unable to read current working
  directory`). This killed the session that did this investigation. Measure
  from a scratch worktree, or from the main checkout with `--no-worktree`.
- `python3` in a worktree can fail through the asdf shim (no version set).
  Use `/usr/bin/python3` for timing helpers.
