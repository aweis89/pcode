"""One write/read policy for the file tools and the model's shell commands.

The bundled `security` extension enforces it twice: a tool hook refuses file-tool
writes (and reads of credential files) the policy rejects, and every `shell`
command's job supervisor runs under an OS sandbox generated from the same
policy (Seatbelt on macOS, bubblewrap on Linux), so the two cannot drift.

Writes are allowed under the write roots: the workspace, its repository's main
checkout (which holds every `.worktrees/` sibling), temp and cache directories,
entries in `security.json`, and session grants from `/add-dir`. pcode's config
directory, any `.pcode/` directory and any `.git/hooks/` stay read-only even
inside a root, because writing there would let the model disable the extension
or run code outside the sandbox later. Only a root granted at or below one of
those paths reopens it. Reads are open except a deny list of credential files.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pcode.preferences import config_dir

# Process-wide so a grant survives `/reload`, which re-imports the extension.
SESSION_GRANTS: list[Path] = []

DEFAULT_DENY_READ = (
    "~/.ssh/id_*",
    "~/.aws",
    "~/.gnupg",
    "~/.netrc",
    "~/.config/gh/hosts.yml",
    "~/.docker/config.json",
    "~/.codex/auth.json",
    "~/.claude/.credentials.json",
)
CACHE_DIRS = ("~/.cache", "~/Library/Caches", "~/.npm")
# Directory names that stay read-only wherever they appear: `.pcode` holds
# project extensions (one named `security` would replace this one) and
# preferences; git hooks run later, outside any sandbox.
GUARDED = ((".pcode",), (".git", "hooks"))
_GUARDED_REGEXES = (r"/\.pcode(/|$)", r"/\.git/hooks(/|$)")
_DEVICE_WRITES = (
    '(literal "/dev/null")',
    '(literal "/dev/zero")',
    '(literal "/dev/dtracehelper")',
    '(regex #"^/dev/tty")',
    '(regex #"^/dev/fd/")',
)
_GLOB = re.compile(r"[*?\[]")
_REGEX_SPECIAL = set(".^$+(){}|\\")


def config_path() -> Path:
    return config_dir() / "security.json"


def load_config() -> dict:
    """`security.json`, or {} when absent. A malformed file raises ValueError."""
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{path}: {error}") from error
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return data


def add_global_grant(path: Path) -> None:
    """Append `path` to `security.json`'s write list, for every future session."""
    data = load_config()
    write = [str(entry) for entry in data.get("write", [])]
    if str(path) not in write:
        write.append(str(path))
    data["write"] = write
    target = config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    pending = target.with_suffix(".tmp")
    pending.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    pending.replace(target)


def real(path: str | Path, base: Path | None = None) -> Path:
    """Absolute, `~`-expanded, symlink-resolved; relative paths join `base`."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() and base is not None:
        candidate = base / candidate
    return Path(os.path.realpath(candidate))


def within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def base_roots(workspace: Path) -> list[Path]:
    """The roots every session starts with. Computed once: it shells out to git."""
    from pcode.worktree import repo_scope

    workspace = real(workspace)
    roots = [workspace, real(repo_scope(workspace))]
    temp = real(tempfile.gettempdir())
    roots += [temp, real("/tmp"), real("/var/tmp")]
    # macOS keeps a per-user cache dir (`.../C`) beside `$TMPDIR` (`.../T`).
    if temp.parent.is_relative_to("/private/var/folders"):
        roots.append(temp.parent)
    roots += [real(entry) for entry in CACHE_DIRS]
    return _unique(roots)


def glob_regex(pattern: str) -> str:
    """A path glob as an anchored regex matching it and anything beneath it.

    `*` and `?` stay within one path component. The same text is valid in
    Python and in Seatbelt's `(regex #"...")`.
    """
    out = []
    for char in pattern:
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char in _REGEX_SPECIAL:
            out.append("\\" + char)
        else:
            out.append(char)
    return "^" + "".join(out) + "(/|$)"


def _expand_pattern(pattern: str) -> str:
    """`~` expanded and the literal directory part symlink-resolved."""
    pattern = os.path.expanduser(pattern)
    match = _GLOB.search(pattern)
    if match is None:
        return str(real(pattern))
    head, _, tail = pattern[: match.start()].rpartition("/")
    return str(real(head or "/")).rstrip("/") + "/" + tail + pattern[match.start() :]


@dataclass
class Policy:
    write: list[Path]
    protected: list[Path]
    deny_read: list[str]

    @classmethod
    def build(
        cls,
        base: Iterable[Path],
        config: dict | None = None,
        grants: Iterable[Path] = (),
    ) -> Policy:
        config = load_config() if config is None else config
        home = config_dir()
        deny = config.get("deny_read")
        if deny is None:
            deny = [
                *DEFAULT_DENY_READ,
                str(home / "credentials.json"),
                str(home / "mcp-credentials.json"),
            ]
        return cls(
            write=_unique([*base, *(real(entry) for entry in config.get("write", [])), *grants]),
            protected=[real(home)],
            deny_read=[_expand_pattern(str(entry)) for entry in deny],
        )

    # -- decisions -----------------------------------------------------------

    def guards(self, path: Path) -> list[Path]:
        """The read-only zones `path` falls in."""
        found = [zone for zone in self.protected if within(path, zone)]
        parts = path.parts
        for names in GUARDED:
            for index in range(len(parts) - len(names) + 1):
                if parts[index : index + len(names)] == names:
                    found.append(Path(*parts[: index + len(names)]))
        return found

    def can_write(self, path: Path) -> bool:
        roots = [root for root in self.write if within(path, root)]
        if not roots:
            return False
        # A guarded zone is writable only through a root granted inside it.
        return all(any(within(root, zone) for root in roots) for zone in self.guards(path))

    def can_read(self, path: Path) -> bool:
        text = str(path)
        return not any(re.match(glob_regex(pattern), text) for pattern in self.deny_read)

    def explain(self) -> str:
        roots = ", ".join(str(root) for root in self.write)
        return f"Writable: {roots}."

    # -- OS sandboxes --------------------------------------------------------

    def seatbelt_profile(self, extra_write: Sequence[Path] = ()) -> str:
        roots = [*self.write, *(real(path) for path in extra_write)]
        reopened = [root for root in roots if self.guards(root)]
        lines = [
            "(version 1)",
            "(allow default)",
            "(deny file-write*)",
            _rule("allow file-write*", [*map(_filter, roots), *_DEVICE_WRITES]),
            _rule(
                "deny file-write*",
                [
                    *(f"(subpath {_quote(zone)})" for zone in self.protected),
                    *(f'(regex #"{regex}")' for regex in _GUARDED_REGEXES),
                ],
            ),
        ]
        if reopened:
            lines.append(_rule("allow file-write*", [_filter(root) for root in reopened]))
        if self.deny_read:
            lines.append(
                _rule(
                    "deny file-read*",
                    [f'(regex #"{glob_regex(pattern)}")' for pattern in self.deny_read],
                )
            )
        return "\n".join(lines)

    def bwrap_args(self, extra_write: Sequence[Path] = ()) -> list[str]:
        """Linux equivalent. Guarded names are only protected where they exist now."""
        roots = [path for path in (*self.write, *map(real, extra_write)) if path.exists()]
        args = ["--ro-bind", "/", "/", "--dev-bind", "/dev", "/dev", "--proc", "/proc"]
        for root in roots:
            args += ["--bind", str(root), str(root)]
        zones = [zone for zone in self.protected if zone.exists()]
        for root in roots:
            if root.is_dir():
                zones += [zone for names in GUARDED if (zone := root.joinpath(*names)).exists()]
        for zone in zones:
            args += ["--ro-bind", str(zone), str(zone)]
        for root in roots:
            if self.guards(root):
                args += ["--bind", str(root), str(root)]
        for pattern in self.deny_read:
            for match in sorted(glob.glob(pattern)):
                if os.path.isdir(match):
                    args += ["--tmpfs", match]
                else:
                    args += ["--ro-bind", "/dev/null", match]
        return args


def backend() -> str | None:
    """Which OS sandbox this machine offers, or None."""
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return "seatbelt"
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        return "bwrap"
    return None


def command_prefix(policy: Policy, job_directory: Path) -> list[str]:
    """The argv that runs a job supervisor inside the sandbox.

    The job directory is writable so the supervisor can publish output and
    status; nothing else beyond the policy is.
    """
    kind = backend()
    if kind == "seatbelt":
        return [
            shutil.which("sandbox-exec") or "sandbox-exec",
            "-p",
            policy.seatbelt_profile([job_directory]),
        ]
    if kind == "bwrap":
        return [shutil.which("bwrap") or "bwrap", *policy.bwrap_args([job_directory])]
    raise RuntimeError("No OS sandbox is available (sandbox-exec on macOS, bwrap on Linux).")


def _filter(path: Path) -> str:
    kind = "literal" if path.is_file() else "subpath"
    return f"({kind} {_quote(path)})"


def _quote(path: Path | str) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _rule(head: str, filters: list[str]) -> str:
    return f"({head} " + " ".join(filters) + ")"


def _unique(paths: Iterable[Path]) -> list[Path]:
    return list(dict.fromkeys(paths))
