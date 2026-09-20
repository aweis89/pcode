"""Which repositories may run the code they ship: `.pcode/extensions` and `.pcode/worktree-setup`.

Both execute at launch with the user's permissions, so a freshly cloned
repository must not get to run them just because pcode was started inside it.
Trust is granted per repository, once, by answering the launch prompt; the
answer lands in the user-only `trusted_projects` setting, keyed by the primary
checkout so every worktree of a trusted repository is covered.
`project_extensions on` remains the blanket opt-in.
"""

import os
from pathlib import Path

from pcode.preferences import SETTINGS, load_preferences, update_preferences

EXTENSIONS_DIR = Path(".pcode") / "extensions"
SETUP_SCRIPT = Path(".pcode") / "worktree-setup"


def trust_key(workspace: Path) -> Path:
    """The path trust is recorded under: the primary checkout of a git repo."""
    from pcode.worktree import main_checkout

    workspace = workspace.resolve()
    return main_checkout(workspace) or workspace


def project_code(workspace: Path) -> list[Path]:
    """Launch-time code the repository ships, relative to its root, or empty."""
    found = []
    extensions = workspace / EXTENSIONS_DIR
    if extensions.is_dir() and any(not p.name.startswith(("_", ".")) for p in extensions.iterdir()):
        found.append(EXTENSIONS_DIR)
    if (workspace / SETUP_SCRIPT).is_file():
        found.append(SETUP_SCRIPT)
    return found


def trusted_projects() -> set[Path]:
    raw = load_preferences().get("trusted_projects", SETTINGS["trusted_projects"].default) or ""
    return {Path(entry).expanduser() for entry in raw.split(os.pathsep) if entry.strip()}


def is_trusted(workspace: Path) -> bool:
    if load_preferences().get("project_extensions", "off") == "on":
        return True
    return trust_key(workspace) in trusted_projects()


def trust(workspace: Path) -> Path:
    """Record trust for the repository containing `workspace`; returns the recorded key."""
    key = trust_key(workspace)
    entries = trusted_projects() | {key}
    update_preferences({"trusted_projects": os.pathsep.join(str(p) for p in sorted(entries))})
    return key


def prompt_trust(workspace: Path, ask=input, stream=None) -> bool:
    """Ask once, at launch, whether a repository shipping code may run it.

    Returns whether it is trusted afterwards. No prompt when nothing is
    shipped, when already trusted, or when `ask` is None (non-interactive):
    then the code is skipped and a note says so.
    """
    import sys

    stream = stream or sys.stderr
    code = project_code(workspace)
    if not code:
        return False
    if is_trusted(workspace):
        return True
    key = trust_key(workspace)
    listed = ", ".join(str(p) for p in code)
    if ask is None:
        print(f"pcode: skipping untrusted project code in {key}: {listed}", file=stream)
        return False
    print(
        f"pcode: {key} ships code that runs at launch with your permissions: {listed}",
        file=stream,
    )
    try:
        answer = ask("Trust this repository? [y/N] ")
    except (EOFError, OSError, KeyboardInterrupt):
        answer = ""  # closed or captured stdin: same as declining
    if answer.strip().lower() in ("y", "yes"):
        trust(workspace)
        print(
            "pcode: trusted; revoke with `pcode config unset trusted_projects` "
            "or edit that setting.",
            file=stream,
        )
        return True
    print("pcode: not trusted; that code is skipped this launch.", file=stream)
    return False
