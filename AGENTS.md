# Development notes

## Hard rules

- **Always commit and merge to `master` when done.** The global `pcode` is an editable install pointing at the mainline `src/`, so a merged change is live on the next start with no reinstall. When working in a worktree, merge the finished branch into `master` once tests pass (as long as `master` is clean); only leave it unmerged if `master` has uncommitted changes or the user asked to hold off, and call that out prominently at the top of your final message.
- **Never `git stash` here.** Dozens of worktrees share one stash ref, so a concurrent session's push or pop can hijack yours and land as conflicts. Commit to a scratch branch instead.
- **Never run `make install` from a worktree** — it repoints the global `pcode` at that branch.
- **Never bypass `make install` with bare `uv tool install`.** `uv tool install` ignores `uv.lock` and resolves afresh (anthropic 2.x once broke every turn while `make test` passed); `make install` constrains it to `uv.lock`.
- **Delegate a sub-agent review** after committing any non-trivial change (pass the commit range and the intent, since it doesn't see this conversation) and address its findings in follow-up commits before calling the work done. Commit first: a dirty `master` blocks other sessions.
- **Run `make test-all`** before committing anything touching layout, streaming, the editor, or the prompt (`make test` skips the real-tmux regressions, which are 75% of the runtime).

## Commands and environment

- **Dependency bumps:** Only `pyproject.toml`/`uv.lock` changes need `make install`. `.githooks/post-merge` runs it on the mainline when a merge touches them (needs `git config core.hooksPath .githooks`), but it only fires on merge — editing a dependency directly on mainline breaks the global `pcode` with an `ImportError` until you run `make install`. Bump dependencies in a worktree, or run `make install` immediately when `uv.lock` and the code that needs it change.
- **Git email and `make sync`:** `GIT_AUTHOR_EMAIL`/`GIT_COMMITTER_EMAIL` from a parent `.envrc` beat `user.email`, which can leak a work address into public history. `.githooks/pre-commit` and `pre-push` block that once local `user.email` is set; `make sync` sets both it and `core.hooksPath`. After any rewrite of `origin`'s history, other clones must run `make sync` (or `git fetch origin && git show origin/master:scripts/sync.sh | sh`) before pulling — a plain `git pull` merges old commits back and `post-merge` pushes them.
- **Parallel `uv run` and pytest:** Do not run another syncing `uv run` beside pytest in the same environment — it can briefly uninstall Harness while tests read its metadata (`PackageNotFoundError`). Use `uv run --no-sync` for concurrent read-only checks, or run serially.
- **Stable tree for `make test`:** `make test` is xdist-parallel; saving a file mid-run yields bulk failures or `Different tests were collected between gw0 and gwN`. Re-run on a quiet tree (or `uv run pytest -n0`) before believing a mass failure.
- **Worker cap:** `-n auto` is capped at 4 workers (`tests/conftest.py`) because concurrent sessions exhausted RAM and swap. Don't pass `-n <cores>` to bypass it; on an otherwise idle machine, use `PYTEST_XDIST_AUTO_NUM_WORKERS=<n>`.
- **Pinned Harness source:** `make harness-src` checks out the Pydantic AI repo (Harness source in `src/pydantic_ai_harness/pydantic_ai_harness/`, plus `docs/harness/` and `tests/harness/`) at the locked release under `tmp/pydantic-ai`. Read that rather than the website (which can describe unreleased APIs). The old `pydantic/pydantic-ai-harness` repo is frozen; don't read it.

## Logs and debugging

- **Stale modules in running sessions:** `make install` is editable, so a running session keeps whatever source was on disk when each module was first imported; a mid-turn fix does not reach it until restart. Before hunting an unreproducible failure, compare its timestamp against file mtimes and check whether another session was editing.
- **Failed turns:** Full frames are saved to `<session-dir>/errors.log`; the transcript's `turn_failed` record carries only a type and message.
- **Library warnings:** `logging` warnings and `warnings.warn` never reach the terminal — `library_log.install()` sends `WARNING` and above (plus `ANTHROPIC_LOG=debug` etc.) to `~/.local/state/pcode/library.log` (or the host log for hosted sessions).

## References

- **Terminal or agent integrations:** Read [dev/dependencies.md](dev/dependencies.md) before touching them.
- **Tmux, socket, and terminal tests:** Read [dev/testing.md](dev/testing.md) for timing traps, `make test-socket`, `test_terminal.py` worktree quirks, throwaway worktree comparisons, `--no-worktree`, and CPU load burners.
- **Docs vs. `dev/`:** `docs/` is the published user site (`zensical.toml`): what a feature does and why you'd want it, not how it's built. Design notes, plans, runbooks, and implementation detail go in `dev/` (unpublished) or code comments. A published page linking into `dev/` must use a full GitHub URL. `make docs` checks links/anchors under `docs/` only (not `dev/` or `README`).
