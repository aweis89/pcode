# Development notes

## Workflow

- Always commit after changes. The global `pcode` is an editable install pointing at the mainline `src/`, so a merged change is live on the next start with no reinstall. Only `pyproject.toml`/`uv.lock` changes need `make install`, and the `.githooks/post-merge` hook runs it on the mainline when a merge touches them (needs `git config core.hooksPath .githooks`). Never run `make install` from a worktree: it repoints the global `pcode` at that branch.
- Before touching terminal or agent integrations, read [docs/dependencies.md](docs/dependencies.md).
- User-facing behavior is documented under `docs/` (one page per topic, listed in `zensical.toml`), not the README, which is only a landing page. `make docs` fails on a broken page or anchor link, so run it after moving or renaming a heading.
- `make harness-src` checks out Harness upstream source, docs, and tests at the pinned SHA under `tmp/pydantic-ai-harness`. Read that rather than the website, which can describe an unreleased Coder API and extras.

## Testing and debugging

- `make test` skips the real-tmux regressions (75% of the runtime). Run `make test-all` before pushing anything touching layout, streaming, the editor, or the prompt. Never parallelize the tmux tests.
- `make test` is xdist-parallel and needs a stable tree: saving a file mid-run yields bulk failures or `Different tests were collected between gw0 and gwN`. Re-run on a quiet tree (or `uv run pytest -n0`) before believing a mass failure.
- `make install` is editable, so a running session keeps whatever source was on disk when each module was first imported: a mid-turn fix does not reach it, and a broken intermediate state stays loaded until restart. Before hunting an unreproducible failure, compare its timestamp against file mtimes and check whether another session was editing.
- Failed turns save their frames to `<session-dir>/errors.log`; the transcript's `turn_failed` record carries only a type and message.
