# Development notes

- Commit code changes after making them.

- Before changing terminal or agent integrations, consult [the dependency reference guide](docs/dependencies.md) for official docs, installed-source discovery, and version-verification guidance.

- A PTY with `PROMPT_TOOLKIT_NO_CPR=1` does not exercise real prompt height: cursor-position reports can make the layout stretch into the remaining pane. Keep the real-tmux height regression tests, not just PTY startup/exit checks.
- Harness's latest website can describe an unreleased Coder API and extras; verify the installed release's signatures/tool composition instead of assuming the website matches PyPI.
