---
name: self-improve
description: Change pcode itself or write a pcode extension. Use before touching src/pcode, a Harness or Pydantic AI API, or a file under any extensions/ directory, and before answering questions about how pcode's agent is wired.
---

# Self-improve

pcode is a thin layer over Pydantic AI and Pydantic AI Harness. Most mistakes
here come from writing a hook, tool, or capability from memory against an API
that moved. Read the pinned docs first; they are on disk or one fetch away.

## Extensions

Read `src/pcode/extension_guide.md` first; it is the whole pcode-side API
(`tool`, `instructions`, `hooks`, `add_capability`, `subagent`,
`register_command`). The bundled files in `src/pcode/extensions/` are working
examples. Use the Pydantic AI docs below for what the guide only names: hook
signatures, `RunContext`, `ModelRetry`, capability classes, sub-agent `Agent`
construction.

After editing an extension, check it imports, then `/reload`:

```sh
uv run python -c "import runpy; runpy.run_path('path/to/ext.py')"
```

## Pinned docs

The website and `main` describe APIs this checkout may not have. Read the
version we actually run.

Harness (source, docs, and tests at the pinned SHA):

```sh
make harness-src        # prints "tmp/pydantic-ai-harness @ <sha>"
```

| Need | Read |
| --- | --- |
| Coder, FileSystem, Shell, SubAgents, Planning, compaction, step persistence | `tmp/pydantic-ai-harness/docs/<name>.md` |
| Exact behaviour of a capability | `tmp/pydantic-ai-harness/pydantic_ai_harness/<name>/` and `tests/` beside it |

Pydantic AI: docs are not installed with the package. Fetch them at the tag
pinned in `uv.lock` (the `pydantic-ai-slim` version, prefixed with `v`):

```sh
grep -A1 '^name = "pydantic-ai-slim"' uv.lock | tail -1
```

| Topic | URL (replace `vX.Y.Z`) |
| --- | --- |
| hooks | `https://raw.githubusercontent.com/pydantic/pydantic-ai/vX.Y.Z/docs/hooks.md` |
| tools, tools-advanced, toolsets, agent | `.../docs/<topic>.md` |
| capabilities | `.../docs/capabilities/overview.md` and `capabilities/custom.md` (there is no `capabilities.md`) |

Source is installed: `.venv/lib/python*/site-packages/pydantic_ai/`. When a
doc and the source disagree, the source wins.

## Working on pcode

- Edit in a worktree, never the mainline checkout; `pcode --worktree` sessions
  already are one. Commit after every change.
- `make test` for most changes; `make test-all` before anything touching
  layout, streaming, the editor, or the prompt. Save all files before running:
  xdist collects mid-edit trees inconsistently.
- The running session imported the source that was on disk at first import. A
  fix to `src/pcode` is not live until pcode restarts, and a mid-turn edit to
  an extension is not live until `/reload`. Do not debug "the fix did nothing"
  before checking that.
- Never `make install` from a worktree; it repoints the global `pcode` at the
  branch.
- Anything in the cached prompt prefix (`instructions`, tool schemas) must be
  stable across turns. Verify caching changes with the `cache-report` skill.
- Narrower traps live as comments beside the code: hook and capability-id
  semantics in `agent.py` and `cache_warnings.py`, terminal handoff in
  `ui.suspended_editor`, resume semantics in `live.py`.
