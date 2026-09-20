# Development notes

## Workflow

Several agents edit this repo concurrently. Work in a worktree, never the
mainline checkout, where another session's `git checkout`/`stash`/`reset` can
eat your uncommitted edits:

```bash
make worktree NAME=fix-thing          # .worktrees/fix-thing, branch fix-thing, own .venv
cd .worktrees/fix-thing               # edit, make test, commit here
make worktree-merge NAME=fix-thing    # merge mainline into the branch, then ff mainline
make worktree-remove NAME=fix-thing   # drop the worktree (branch is kept)
```

- Conflicts surface in the worktree, never on mainline. The only failure needing coordination is the final `--ff-only` refusing because someone's uncommitted mainline edits touch your files: have them commit or stash, then re-run.
- Never share a `.venv` between worktrees: the editable install records an absolute `src/` path, so a shared env silently imports the *other* checkout's source.
- Always commit and push after changes. Run `make install` afterwards from the mainline checkout; running it from a worktree repoints the global `pcode` command at that branch.
- Before touching terminal or agent integrations, read [docs/dependencies.md](docs/dependencies.md).
- `make harness-src` checks out Harness upstream source, docs, and tests at the pinned SHA under `tmp/pydantic-ai-harness`. Read that rather than the website, which can describe an unreleased Coder API and extras. Verify against the installed release.

## Testing

- `make test` skips the real-tmux regressions (75% of the runtime). Run `make test-all` before pushing anything touching layout, streaming, the editor, or the prompt. Never parallelize the tmux tests: their pane-paint deadlines expire under `-n`, even `-n 4`.
- `make test` is xdist-parallel and needs a stable tree: saving a file mid-run yields bulk failures or `Different tests were collected between gw0 and gwN`. Re-run on a quiet tree (or `uv run pytest -n0`) before believing a mass failure.
- `make install` is editable, so a running session keeps whatever source was on disk when each module was first imported: a mid-turn fix does not reach it, and a broken intermediate state stays loaded until restart. Before hunting an unreproducible failure, compare its timestamp against file mtimes and check whether another session was editing. Failed turns save their frames to `<session-dir>/errors.log`; the transcript's `turn_failed` record carries only a type and message.
- Keep the real-tmux height regression tests. A PTY with `PROMPT_TOOLKIT_NO_CPR=1` does not exercise real prompt height, since cursor-position reports can stretch the layout into the remaining pane.
- `capture-pane` only shows the settled frame, so it cannot see flicker (usually two paints inside one 1/30 s `min_redraw_interval`). Record the raw byte stream with timestamps instead (`tmux pipe-pane -o 'python3 stamp.py >> out.bin'`) and look for a second editor paint after a scrollback write. Terminal handoffs go through `suspended_editor`, not prompt_toolkit's `in_terminal`, because `in_terminal` repaints before its CPR reply arrives.
- On Python 3.14, `cProfile` can observe worker threads, so a `time.thread_time` timer produces negative timings. Use Yappi's per-thread CPU accounting for function profiling.

## Harness gotchas

- Instruction parts are attributed to a capability only when it has an `id`, and Harness leaves several prompt-bearing ones anonymous. Name them at construction (`create_coder`, `create_repo_context`): a `Capability` binds its instructions to its id in `__init__`, so a later `capability.id = ...` is silently ignored, and blanket-renaming everything breaks `replace()`-copied children that compare fields with their parent.
- `before_model_request`/`after_model_request` are filters, not listeners: the return value replaces the request context or response, so a hook that only records something must still `return` it. A dropped return surfaces far away as `AttributeError: 'NoneType' object has no attribute 'usage'` from Harness's `WarnOnCacheBusts`.
- Resuming with `Agent.run(None)` when history ends in a final `ModelResponse` can return that saved answer without calling the provider. Retry from the failed request boundary instead.
- A cache-collapse warning ending in the generic `(e.g. a gap longer than the cache TTL)` means the gap was *under* the TTL: Harness names the measured gap when it actually exceeds it. Its `model request N` is per-run, not per-session. Read the `Message N changed` / `Prefix intact` line that `cache_diagnostics.py` appends, and the dumped fingerprints, before theorizing.
