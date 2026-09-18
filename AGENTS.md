# Development notes

- Always commit code changes after making them, and push them.

- Run `make install` after changes so the installed `pcode` tool env picks them up.

- Before changing terminal or agent integrations, consult [the dependency reference guide](docs/dependencies.md) for official docs, installed-source discovery, and version-verification guidance.

- Run `make harness-src` to get Harness upstream source, docs, tests, and examples at the pinned SHA under `tmp/pydantic-ai-harness` (gitignored), then read that instead of searching the web. It is idempotent and safe to run whenever you are unsure the checkout is current — see "Local Harness checkout" in the dependency guide.

- A PTY with `PROMPT_TOOLKIT_NO_CPR=1` does not exercise real prompt height: cursor-position reports can make the layout stretch into the remaining pane. Keep the real-tmux height regression tests, not just PTY startup/exit checks.
- Harness's latest website can describe an unreleased Coder API and extras; verify the installed release's signatures/tool composition instead of assuming the website matches PyPI.

- Resuming with `Agent.run(None)` and history ending in a final `ModelResponse` can return that saved answer without calling the provider; retry from the failed request boundary instead.

- On Python 3.14, `cProfile` can observe worker threads too: using `time.thread_time` as its timer produces negative/nonsensical timings. Use Yappi's per-thread CPU accounting for function profiling, not a custom `cProfile` CPU clock.
