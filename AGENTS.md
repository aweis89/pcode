# Development notes

- Always commit code changes after making them, and push them.

- Run `make install` after changes so the installed `pcode` tool env picks them up.

- Before changing terminal or agent integrations, consult [the dependency reference guide](docs/dependencies.md) for official docs, installed-source discovery, and version-verification guidance.

- Run `make harness-src` to get Harness upstream source, docs, tests, and examples at the pinned SHA under `tmp/pydantic-ai-harness` (gitignored), then read that instead of searching the web. It is idempotent and safe to run whenever you are unsure the checkout is current — see "Local Harness checkout" in the dependency guide.

- `make test` skips the real-tmux regressions (they are 75% of the suite's runtime). Run `make test-all` before pushing anything touching layout, streaming, the editor, or the prompt. Do not parallelize the tmux tests: under `-n auto` they fail in bulk because their pane-paint deadlines expire, and even `-n 4`/`-n 8` flake.

- `make test` is xdist-parallel, so it needs a stable tree for its duration: saving a source or test file mid-run yields bulk failures or a `Different tests were collected between gw0 and gwN` collection error, neither of which means the change is broken. Re-run on a quiet tree (or `uv run pytest -n0`) before believing a mass failure.

- A PTY with `PROMPT_TOOLKIT_NO_CPR=1` does not exercise real prompt height: cursor-position reports can make the layout stretch into the remaining pane. Keep the real-tmux height regression tests, not just PTY startup/exit checks.
- Harness's latest website can describe an unreleased Coder API and extras; verify the installed release's signatures/tool composition instead of assuming the website matches PyPI.

- Resuming with `Agent.run(None)` and history ending in a final `ModelResponse` can return that saved answer without calling the provider; retry from the failed request boundary instead.

- On Python 3.14, `cProfile` can observe worker threads too: using `time.thread_time` as its timer produces negative/nonsensical timings. Use Yappi's per-thread CPU accounting for function profiling, not a custom `cProfile` CPU clock.

- A cache-collapse warning ending in the generic `(e.g. a gap longer than the cache TTL)` means the gap was *under* the TTL: Harness names the measured gap whenever it actually exceeds it, so that phrasing rules expiry out rather than suggesting it. Its `model request N` is also per-run, not per-session. Read the `Message N changed` / `Prefix intact` line that `cache_diagnostics.py` appends, and the dumped fingerprints, before theorizing about a cause.
