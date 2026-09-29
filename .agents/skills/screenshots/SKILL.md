---
name: screenshots
description: Create or regenerate the docs screenshots in docs/assets/screenshots from scripted pcode sessions. Use when adding a screenshot to the docs, when a UI change makes the existing ones stale, or when asked to show a feature in a specific state without driving a real model.
---

# Screenshots

Docs screenshots come from scenes in `scripts/screenshots/scenes/`, never from a
real model session. A scene runs the real pcode terminal (real tools, real
status line, real popups) on a scripted model, in a private tmux server, inside
a throwaway `acme-api` git repo at `/tmp/pcode-demo` with HOME and the XDG
directories pointed there. Your own sessions, preferences and logins are never
touched, and nothing costs anything.

```sh
make screenshots                       # every scene
make screenshots SCENES="tree jobs"    # some scenes
uv run --no-sync python scripts/screenshots/run.py --text tools   # also print each shot as text
```

Each `("shot", name)` step writes `docs/assets/screenshots/<name>.svg`. You
can't look at an SVG, so run with `--text` and read the plain-text dump to
check a shot shows what the scene meant it to.

## Writing a scene

Copy the closest existing scene. A scene file has:

- `TURNS`: prompt substring → the list of model responses for that turn, one
  per model request. A response is a list of parts: a `str` (reply text,
  Markdown), `Think("...")` (readable reasoning), or `Call(tool, **args)` with
  the tool's real arguments (`shell` takes `command`, `background`,
  `purpose`; `edit_file` takes `path`, `old_text`, `new_text`). pcode sends a
  new request after each round of tool results, so a response with calls is
  followed by the next response in the list.
- `STEPS`: `("type", text)`, `("key", tmux-key)` (`Enter`, `C-g`, `Up`,
  `Escape`), `("wait", text)`, `("sleep", seconds)`, `("shot", name, title)`.
- Optionally `SIZE` (columns, rows; default 100×30), `MODEL` (shown on the
  status line; the scripted model stands in for whatever it names) and
  `PREFERENCES` (saved before launch, e.g. `{"show_thinking": "on"}`).
- The `if __name__ == "__main__": launch(TURNS, model=MODEL, ...)` footer.

The demo repo's files are `DEMO_FILES` in `run.py`. `acme/orders.py` has a
deliberate bug (a percentage discount subtracted as a flat amount) that
scenes fix.

## Traps

- Always `("wait", ...)` for text the step produces before the next step or a
  shot. A shot also waits for the screen to stop changing, but that can't
  tell a finished turn from one that hasn't started.
- Wait on rendered text, not Markdown source: backticks and `**` are gone on
  screen. Whitespace doesn't matter, since wait text may wrap.
- Tool calls really run in the demo repo, so a failing command must really
  fail (the `tools` scene's `python3 -c ... assert` does). Use commands that
  exist on a bare machine; `pytest` isn't installed in the demo repo.
- Background jobs outlive pcode by design. The runner stops any job whose
  supervisor runs under `/tmp/pcode-demo` when a scene ends; if a scene is
  interrupted, `pgrep -fl pcode-demo` finds leftovers.
- `launch()` patches `pcode.agent.resolve_model`, so the terminal must run
  in-process (`--no-host`, which `launch()` passes). A model resolved some
  other way isn't scripted. `/btw` goes through `resolve_model`; delegated
  workers haven't been tried yet, so check a `/workers` scene with `--text`.
