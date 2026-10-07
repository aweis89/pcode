# Contributing

Bug reports and small fixes are the most useful thing right now. For anything
larger, open an issue first so we can agree on the shape before you build it.

## Setup

```sh
git clone https://github.com/cruxwell/pcode.git
cd pcode
make run         # run from source without installing (needs uv)
```

`make install` puts an editable `pcode` on your PATH, so source edits are live
on the next start.

## Checks

```sh
make lint        # ruff check and format check (make fmt fixes)
make test        # fast suite, the same one CI runs
make test-all    # adds the real-tmux and socket-transport runs
```

Run `make test-all` before sending changes to layout, streaming, the editor or
the prompt: those regressions only show up in a real terminal, and CI doesn't
run them.

## Pull requests

- Use a semantic title and commit messages (`fix: ...`, `feat: ...`,
  `docs: ...`).
- Add a test with the fix. Most bugs can be reproduced with a scripted model;
  see the existing tests for examples.
- User-facing behavior goes in `docs/`; design notes go in `dev/`.

[AGENTS.md](AGENTS.md) lists the traps in this repo, such as tmux tests that
look flaky and launching pcode from inside the checkout. It's written for coding
agents but is just as useful to people.
