# Development notes

- Always commit code changes after making them, and push them.

- Run `make install` after changes so the installed `pcode` tool env picks them up.

- Before changing terminal or agent integrations, consult [the dependency reference guide](docs/dependencies.md) for official docs, installed-source discovery, and version-verification guidance.

- Harness upstream source, docs, tests, and examples are already cloned at `~/p/pydantic-ai-harness`; read it instead of searching the web. Check its `HEAD` against the SHA pin in `pyproject.toml` first — see "Local Harness checkout" in the dependency guide.

- A PTY with `PROMPT_TOOLKIT_NO_CPR=1` does not exercise real prompt height: cursor-position reports can make the layout stretch into the remaining pane. Keep the real-tmux height regression tests, not just PTY startup/exit checks.
- Harness's latest website can describe an unreleased Coder API and extras; verify the installed release's signatures/tool composition instead of assuming the website matches PyPI.

- Resuming with `Agent.run(None)` and history ending in a final `ModelResponse` can return that saved answer without calling the provider; retry from the failed request boundary instead.
