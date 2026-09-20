# Development notes

## Workflow

Several agents edit this repo concurrently. Work in a worktree, never the
mainline checkout, where another session's `git checkout`/`stash`/`reset` can
eat your uncommitted edits. pcode sessions started with `--worktree` (or
`worktree on`) already are one: the workspace *is* `.worktrees/pcode-<name>`, so
just edit, `make test`, and commit. Merging back happens when the session
exits (or the user runs `/worktree finish`); the model does not merge. From
another agent, or by hand:

```bash
make worktree NAME=fix-thing          # .worktrees/fix-thing, branch fix-thing, own .venv
cd .worktrees/fix-thing               # edit, make test, commit here
make worktree-merge NAME=fix-thing    # merge mainline into the branch, then ff mainline
make worktree-remove NAME=fix-thing   # drop the worktree (branch is kept)
```

- The only failure needing coordination is the final `--ff-only` refusing because someone's uncommitted mainline edits touch your files: have them commit or stash, then re-run.
- Two branches that are each green can merge without a conflict and still leave mainline broken: one renamed `--demo`'s dest to `theme_preview` while the other added a read of `args.demo`, and every interactive launch died on startup. Git cannot see that; run `make test` on the merged tree (in the worktree, after `worktree-merge` folds mainline in) before trusting the merge.
- `.pcode/worktree-setup` is what gives each worktree its own `.venv`. Never share one: the editable install records an absolute `src/` path, so a shared env silently imports the *other* checkout's source.
- Always commit after changes. The global `pcode` is an editable install pointing at the mainline `src/`, so a merged change is live on the next start with no reinstall. Only `pyproject.toml`/`uv.lock` changes need `make install`, and the `.githooks/post-merge` hook runs it on the mainline when a merge touches them (needs `git config core.hooksPath .githooks`). Never run `make install` from a worktree: it repoints the global `pcode` at that branch.
- Before touching terminal or agent integrations, read [docs/dependencies.md](docs/dependencies.md).
- `make harness-src` checks out Harness upstream source, docs, and tests at the pinned SHA under `tmp/pydantic-ai-harness`. Read that rather than the website, which can describe an unreleased Coder API and extras.

## Testing and debugging

- `make test` skips the real-tmux regressions (75% of the runtime). Run `make test-all` before pushing anything touching layout, streaming, the editor, or the prompt. Never parallelize the tmux tests.
- `make test` is xdist-parallel and needs a stable tree: saving a file mid-run yields bulk failures or `Different tests were collected between gw0 and gwN`. Re-run on a quiet tree (or `uv run pytest -n0`) before believing a mass failure.
- `make install` is editable, so a running session keeps whatever source was on disk when each module was first imported: a mid-turn fix does not reach it, and a broken intermediate state stays loaded until restart. Before hunting an unreproducible failure, compare its timestamp against file mtimes and check whether another session was editing.
- Failed turns save their frames to `<session-dir>/errors.log`; the transcript's `turn_failed` record carries only a type and message.

Narrower traps live as comments next to the code they concern: Harness hook
and capability-id semantics in `agent.py` and `cache_warnings.py`, cache-collapse
warning reading in `cache_diagnostics.divergence`, terminal handoff and flicker
in `ui.suspended_editor`, resume semantics in `live.py`, profiler choice in
`profiling.py`, and why the tmux tests exist in `tests/test_tmux.py`.
