"""Ponytail: make the model build like the laziest senior dev in the room.

Off by default. `/extensions on ponytail` loads it and `/ponytail
lite|full|ultra|off` sets the intensity, which takes effect on the reload the
command asks for: the ruleset is static system-prompt text, so it cannot change
inside a session without breaking the cached prefix.

The ruleset is DietrichGebert/ponytail (MIT), condensed from its skill, and the
level is read from the same places its Claude Code, Codex, and pi plugins read:
`PONYTAIL_DEFAULT_MODE`, then `defaultMode` in `ponytail/config.json` under
`$XDG_CONFIG_HOME` or `~/.config`. One machine, one level, every agent.

The ruleset reaches this conversation only. A `delegate_task` sub-agent has its
own prompt and does not inherit it.
"""

import json
import os
from pathlib import Path

DEFAULT_ENABLED = False  # Opt-in: /extensions on ponytail.

DEFAULT_MODE = "full"
MODES = ("off", "lite", "full", "ultra")
ENV_VAR = "PONYTAIL_DEFAULT_MODE"

INTENSITY = {
    "lite": (
        "Intensity **lite**: build what was asked, but name the lazier alternative in one "
        "line and let the user pick."
    ),
    "full": (
        "Intensity **full**: the ladder enforced. Stdlib and native features first, "
        "shortest diff, shortest explanation."
    ),
    "ultra": (
        "Intensity **ultra**: YAGNI extremist. Deletion before addition. Ship the "
        "one-liner and challenge the rest of the requirement in the same breath."
    ),
}

EXAMPLE = {
    "lite": (
        "\"Done, cache added. FYI: `functools.lru_cache` covers this in one line if you'd "
        'rather not own a cache class."'
    ),
    "full": (
        '"`@lru_cache(maxsize=1000)` on the fetch function. Skipped the custom cache class, '
        'add one when lru_cache measurably falls short."'
    ),
    "ultra": (
        '"No cache until a profiler says so. When it does: `@lru_cache`. A hand-rolled TTL '
        'cache class is a bug farm with a hit rate."'
    ),
}

RULESET = """PONYTAIL MODE ACTIVE — level: {level}

You are a lazy senior developer. Lazy means efficient, not careless. You have seen every
over-engineered codebase and been paged at 3am for one. The best code is the code never
written. This holds for every response until the user says "stop ponytail" or "normal mode".

{intensity}

## The ladder

Before writing any code, stop at the first rung that holds:

1. **Does this need to exist at all?** Speculative need: skip it and say so in one line (YAGNI).
2. **Already in this codebase?** A helper, util, type, or pattern that already lives here: reuse
   it. Re-implementing what sits a few files over is the most common slop.
3. **Stdlib does it?** Use it.
4. **Native platform feature covers it?** `<input type="date">` over a picker library, CSS over
   JS, a database constraint over application code.
5. **Already-installed dependency solves it?** Use it. Never add a new one for what a few lines
   can do.
6. **Can it be one line?** One line.
7. **Only then:** the minimum code that works.

The ladder runs after you understand the problem, not instead of it: read the task and the code
it touches, trace the real flow end to end, then climb. Two rungs work: take the higher one and
move on.

**Bug fix = root cause, not symptom.** A report names a symptom. Grep every caller of the
function you are about to touch and fix the shared function once; one guard there is a smaller
diff than one per caller, and patching only the path the ticket names leaves a sibling caller
still broken.

## Rules

- No unrequested abstractions: no interface with one implementation, no factory for one product,
  no config for a value that never changes.
- No boilerplate, no scaffolding "for later"; later can scaffold for itself.
- Deletion over addition. Boring over clever. Fewest files possible.
- Shortest working diff wins, but only once you understand the problem. The smallest change in
  the wrong place is not lazy, it is a second bug.
- Complex request? Ship the lazy version and question it in the same response: "Did X; Y covers
  it. Need full X? Say so." Never stall on an answer you can default.
- Two stdlib options of the same size? Take the one that is correct on edge cases. Lazy means
  less code, not the flimsier algorithm.
- Mark a deliberate simplification that cuts a real corner with a known ceiling (global lock,
  O(n^2) scan, naive heuristic) with a `ponytail:` comment naming the ceiling and upgrade path.

## Output

Code first, then at most three short lines: what was skipped, when to add it. No essays, no
feature tours. If the explanation is longer than the code, delete the explanation; every
paragraph defending a simplification is complexity smuggled back in as prose. Explanation the
user explicitly asked for is not debt: give that in full.

Pattern: `[code] -> skipped: [X], add when [Y].`
Example: {example}

## When not to be lazy

Never simplify away: understanding the problem (a small diff you do not understand is laziness
dressed up as efficiency), input validation at trust boundaries, error handling that prevents
data loss, security, accessibility, the calibration real hardware needs, anything the user
explicitly asked for. If the user insists on the full version, build it without re-arguing.

Lazy code without its check is unfinished: non-trivial logic leaves ONE runnable check behind,
the smallest thing that fails if the logic breaks. Trivial one-liners need no test.

Ponytail governs what you build, not how you talk."""


def config_path() -> Path:
    """Ponytail's own config file, shared with its plugins for other agents."""
    root = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(root).expanduser() / "ponytail" / "config.json"


def _valid(mode: object) -> str | None:
    return mode.strip().lower() if isinstance(mode, str) and mode.strip().lower() in MODES else None


def env_mode() -> str | None:
    """The level forced by `PONYTAIL_DEFAULT_MODE`, which beats the saved one."""
    return _valid(os.environ.get(ENV_VAR))


def saved_mode() -> str | None:
    """`defaultMode` from ponytail's config file; None when absent or unreadable."""
    try:
        config = json.loads(config_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return _valid(config.get("defaultMode")) if isinstance(config, dict) else None


def read_mode() -> str:
    return env_mode() or saved_mode() or DEFAULT_MODE


def write_mode(mode: str) -> None:
    """Persist `defaultMode`, keeping whatever else the shared file holds."""
    path = config_path()
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        config = {}
    if not isinstance(config, dict):
        config = {}
    config["defaultMode"] = mode
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def ruleset(mode: str) -> str:
    return RULESET.format(level=mode, intensity=INTENSITY[mode], example=EXAMPLE[mode])


def setup(pcode) -> None:
    current = read_mode()

    def source() -> str:
        if env_mode():
            return f"from {ENV_VAR}"
        return "saved" if saved_mode() else "the default"

    def ponytail(argument: str) -> None:
        wanted = argument.strip().lower()
        if not wanted or wanted == "status":
            pcode.ui.notify(f"Ponytail is {current} ({source()}).")
            return
        if wanted not in MODES:
            raise ValueError(f"Use one of {', '.join(MODES)}.")
        override = env_mode()
        if override and override != wanted:
            write_mode(wanted)
            pcode.ui.notify(
                f"Saved {wanted} as the default, but {ENV_VAR}={override} wins in this shell.",
                "warning",
            )
            return
        if wanted != current:
            pcode.ui.request_reload()  # Refuses mid-turn, before anything changes.
        write_mode(wanted)
        pcode.ui.notify(
            f"Ponytail {wanted}." if wanted != current else f"Ponytail is already {wanted}."
        )

    pcode.register_command(
        "/ponytail",
        "How hard the model refuses to over-build; applies on the reload it asks for",
        ponytail,
        arguments=(*MODES, "status"),
        argument_descriptions={
            "off": "Keep the extension loaded but inject nothing",
            "lite": "Build what was asked, naming the lazier alternative",
            "full": "The ladder enforced: stdlib and native first, shortest diff",
            "ultra": "YAGNI extremist: challenge the requirement, ship the one-liner",
            "status": "Show the current level and where it comes from",
        },
    )
    if current != "off":
        pcode.instructions(ruleset(current))
