"""Complete a filesystem path typed as a slash command's argument.

Only the directory being typed is listed, never walked, and a huge directory
is read only up to `SCAN_LIMIT` entries, so completion stays instant anywhere.
"""

import os
from collections.abc import Iterator
from itertools import islice
from pathlib import Path

from prompt_toolkit.completion import Completion

SCAN_LIMIT = 2000
SHOWN = 100


def path_fragment(argument: str) -> str:
    """The path being typed: the argument after any leading `--option` words."""
    words = argument.lstrip()
    while words.startswith("--"):
        _option, space, words = words.partition(" ")
        if not space:  # Still typing the option itself.
            return ""
        words = words.lstrip()
    return words


def complete_paths(argument: str, workspace: Path) -> Iterator[Completion]:
    """Entries of the typed directory starting with the typed name, directories first.

    Relative paths resolve against `workspace`. Nothing completes until
    something is typed, and a bare `/` does not list the filesystem root.
    """
    fragment = path_fragment(argument)
    if not fragment or fragment == "/":
        return
    if fragment == "~":
        yield Completion("~/", start_position=-1)
        return
    head, _, name = fragment.rpartition("/")
    if "/" in fragment:
        directory = Path(os.path.expanduser(head or "/"))
    else:
        directory = Path()
    if not directory.is_absolute():
        directory = workspace / directory
    needle = name.casefold()
    found: list[tuple[bool, str]] = []
    try:
        with os.scandir(directory) as entries:
            for entry in islice(entries, SCAN_LIMIT):
                if entry.name.startswith(".") and not name.startswith("."):
                    continue
                if entry.name.casefold().startswith(needle):
                    try:
                        is_dir = entry.is_dir()
                    except OSError:
                        is_dir = False
                    found.append((not is_dir, entry.name))
    except OSError:
        return
    found.sort(key=lambda item: (item[0], item[1].casefold()))
    for is_file, entry in found[:SHOWN]:
        suffix = "" if is_file else "/"
        yield Completion(entry + suffix, start_position=-len(name), display=entry + suffix)
