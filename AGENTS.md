# Development notes

## Work in a worktree, not the mainline checkout

Several agents edit this repo at the same time, so editing the mainline working
tree directly means fighting over files edit by edit — expect another session to
commit, `git checkout`, or `git stash` your uncommitted work out from under you.
Branch into a worktree before making changes and merge back when the change is
done:

```bash
make worktree NAME=fix-thing          # .worktrees/fix-thing, branch fix-thing
cd .worktrees/fix-thing               # edit, make test, commit here
make worktree-merge NAME=fix-thing    # merge mainline in, then fast-forward mainline
make worktree-remove NAME=fix-thing   # drop the worktree (branch is kept)
```

- `make worktree-merge` merges the mainline branch **into** the worktree first, so any conflict surfaces in `.worktrees/<name>`, where you are the only writer. Fix it there, commit, and re-run; the mainline tree is never left in a conflicted state.
- The mainline step is `git merge --ff-only`, which git refuses only when someone's uncommitted mainline edits touch the files you merged. That is the one case needing coordination: commit or stash those edits, then re-run the merge.
- Creating a worktree costs a few seconds: it gets its own `.venv` (uv clones the packages from its local cache) and a `tmp` symlink to the shared Harness checkout, so there is nothing to reinstall and `make test` works immediately.
- Do not share `.venv` between worktrees. The editable install records an absolute path to `src/`, so a shared env silently imports the *other* checkout's source and you test code you did not write.
- `make install` from a worktree repoints the global `pcode` command at that worktree. Run it from the mainline checkout after merging, unless you deliberately want the installed command to track your branch.

## Notes

- Always commit code changes after making them, and push them.

- Run `make install` after changes so the installed `pcode` tool env picks them up.

- Before changing terminal or agent integrations, consult [the dependency reference guide](docs/dependencies.md) for official docs, installed-source discovery, and version-verification guidance.

- Run `make harness-src` to get Harness upstream source, docs, tests, and examples at the pinned SHA under `tmp/pydantic-ai-harness` (gitignored), then read that instead of searching the web. It is idempotent and safe to run whenever you are unsure the checkout is current — see "Local Harness checkout" in the dependency guide.

- `make test` skips the real-tmux regressions (they are 75% of the suite's runtime). Run `make test-all` before pushing anything touching layout, streaming, the editor, or the prompt. Do not parallelize the tmux tests: under `-n auto` they fail in bulk because their pane-paint deadlines expire, and even `-n 4`/`-n 8` flake.

- `make test` is xdist-parallel, so it needs a stable tree for its duration: saving a source or test file mid-run yields bulk failures or a `Different tests were collected between gw0 and gwN` collection error, neither of which means the change is broken. Re-run on a quiet tree (or `uv run pytest -n0`) before believing a mass failure.

- `capture-pane` only shows the settled frame, so it cannot see flicker: a flash is usually two paints inside one 1/30 s `min_redraw_interval`. Record the raw byte stream with timestamps instead (`tmux pipe-pane -o 'python3 stamp.py >> out.bin'`) and look for a second editor paint after a scrollback write. Terminal handoffs go through `suspended_editor`, not prompt_toolkit's `in_terminal`, precisely because `in_terminal` repaints before its CPR reply arrives.

- A PTY with `PROMPT_TOOLKIT_NO_CPR=1` does not exercise real prompt height: cursor-position reports can make the layout stretch into the remaining pane. Keep the real-tmux height regression tests, not just PTY startup/exit checks.
- Harness's latest website can describe an unreleased Coder API and extras; verify the installed release's signatures/tool composition instead of assuming the website matches PyPI.

- Resuming with `Agent.run(None)` and history ending in a final `ModelResponse` can return that saved answer without calling the provider; retry from the failed request boundary instead.

- On Python 3.14, `cProfile` can observe worker threads too: using `time.thread_time` as its timer produces negative/nonsensical timings. Use Yappi's per-thread CPU accounting for function profiling, not a custom `cProfile` CPU clock.

- Instruction parts are attributed to a capability only when that capability has an `id`, and Harness leaves several of the ones with prompts anonymous. Name them at construction (`create_coder`, `create_repo_context`): a concrete `Capability` binds its instructions to its id in `__init__`, so a later `capability.id = ...` is silently ignored, and blanket-renaming everything breaks `replace()`-copied children that compare fields with their parent.

- `after_model_request` and `before_model_request` are filters, not listeners: whatever a hook returns replaces the response or request context, so a hook that only records something must still `return` it. Dropping the return blows up somewhere else entirely — a `None` response surfaces as `AttributeError: 'NoneType' object has no attribute 'usage'` from Harness's `WarnOnCacheBusts`, several capabilities downstream of the one at fault.

- `make install` is editable, so a running session keeps whatever source was on disk when each module was first imported — a fix saved mid-turn does not reach it, and a broken intermediate state stays loaded until restart. Before hunting a failure you cannot reproduce, compare its timestamp against `git log`/file mtimes and check whether another session was editing. Failed turns save their frames to `<session-dir>/errors.log`; the transcript's `turn_failed` record carries only a type and a message.

- A cache-collapse warning ending in the generic `(e.g. a gap longer than the cache TTL)` means the gap was *under* the TTL: Harness names the measured gap whenever it actually exceeds it, so that phrasing rules expiry out rather than suggesting it. Its `model request N` is also per-run, not per-session. Read the `Message N changed` / `Prefix intact` line that `cache_diagnostics.py` appends, and the dumped fingerprints, before theorizing about a cause.
