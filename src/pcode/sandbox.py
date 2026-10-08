"""One write/read policy for the file tools and the model's shell commands.

The bundled `sandbox` extension enforces it twice: a tool hook refuses file-tool
writes (and reads of credential files) the policy rejects, and every `shell`
command's job supervisor runs under an OS sandbox generated from the same
policy (Seatbelt on macOS, bubblewrap on Linux), so the two cannot drift.

Writes are allowed under the write roots: the workspace, its repository's main
checkout (which holds every `.worktrees/` sibling), temp, cache and package
directories, entries in `sandbox.json`, and session grants from `/allow-writes`.
pcode's config directory, any `.pcode/` directory and any `.git/hooks/` stay
read-only even inside a root, because writing there would let the model
disable the extension or run code outside the sandbox later. Only a root
granted at or below one of those paths reopens it. Reads are open except a deny
list of credential files.
"""

from __future__ import annotations

import glob
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
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
    "~/.local/share/uv/credentials",
)
CACHE_DIRS = ("~/.cache", "~/Library/Caches", "~/.npm")
# Directory names that stay read-only wherever they appear: `.pcode` holds
# project extensions (one named `sandbox` would replace this one) and
# preferences; git hooks run later, outside any sandbox.
GUARDED = ((".pcode",), (".git", "hooks"))
_GUARDED_REGEXES = (r"/\.pcode(/|$)", r"/\.git/hooks(/|$)")
_DEVICE_WRITES = (
    '(literal "/dev/null")',
    '(literal "/dev/zero")',
    '(literal "/dev/dtracehelper")',
    # Opening a new pseudo-terminal writes /dev/ptmx; without it `script`,
    # `expect` and Python's `pty.openpty()` fail ("out of pty devices").
    '(literal "/dev/ptmx")',
    '(regex #"^/dev/tty")',
    '(regex #"^/dev/fd/")',
)
_GLOB = re.compile(r"[*?\[]")
_REGEX_SPECIAL = set(".^$+(){}|\\")


def config_path() -> Path:
    return config_dir() / "sandbox.json"


def load_config() -> dict:
    """`sandbox.json`, or {} when absent. A malformed file raises ValueError."""
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
    """Append `path` to `sandbox.json`'s write list, for every future session."""
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


def tool_target(path: str, workspace: Path) -> Path:
    """Where a file tool's `path` leads, resolved the way Harness's file tools do.

    Harness joins and normalizes as text first (`Workspace.resolve`: `link/..`
    collapses before any symlink is followed, and `~` is not expanded), then
    follows symlinks. `real` follows `link` before applying `..`, which can name
    a different file than the tool touches.
    """
    joined = posixpath.normpath(posixpath.join(str(workspace), path))
    return Path(os.path.realpath(joined))


def within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def base_roots(workspace: Path) -> list[Path]:
    """The roots every session starts with. Computed once: it shells out to git.

    Under a remote profile the main checkout is not one: its `.git/config`
    would let the model choose code that git runs outside the sandbox (in the
    host, the listener, and the owner's next git command). Only what a
    commit in the session's own worktree needs is writable (`remote_git`).
    """
    from pcode import remote_profile
    from pcode.worktree import repo_scope

    workspace = real(workspace)
    if remote_profile.active() is not None:
        roots = [workspace, *remote_git(workspace)[0]]
    else:
        roots = [workspace, real(repo_scope(workspace))]
    temp = real(tempfile.gettempdir())
    roots += [temp, real("/tmp"), real("/var/tmp")]
    # macOS keeps a per-user cache dir (`.../C`) beside `$TMPDIR` (`.../T`).
    if temp.parent.is_relative_to("/private/var/folders"):
        roots.append(temp.parent)
    roots += [real(entry) for entry in CACHE_DIRS]
    roots += package_stores()
    return _unique(roots)


def remote_git(workspace: Path) -> tuple[list[Path], list[Path]]:
    """(writable, protected) git paths for a remote session's worktree.

    Writable: the object store, the worktree's own admin directory, and its
    branch's ref and reflog (with their lock files). Protected: what tells
    git where the repository is (the worktree's `.git` file, `commondir`,
    `gitdir`) and the per-worktree config, which could name commands to run.
    """
    from pcode.worktree import describe

    tree = describe(workspace)
    if tree is None:
        return [], []

    def git_path(*args: str) -> Path:
        result = subprocess.run(
            ["git", "-C", str(tree.path), "rev-parse", "--path-format=absolute", *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return real(result.stdout.strip())

    common, admin = git_path("--git-common-dir"), git_path("--git-dir")
    writable = [common / "objects", admin]
    if tree.branch and not tree.branch.startswith("("):
        for base in (common / "refs" / "heads", common / "logs" / "refs" / "heads"):
            ref = base / tree.branch
            writable += [ref, ref.with_name(ref.name + ".lock")]
    protected = [
        real(tree.path) / ".git",
        admin / "commondir",
        admin / "gitdir",
        admin / "config.worktree",
    ]
    return writable, protected


def profile_protected(base: Iterable[Path]) -> list[Path]:
    """The git paths a remote session may not write, for the worktree among `base`."""
    for root in base:
        if (root / ".git").exists():
            return remote_git(root)[1]
    return []


def package_stores() -> list[Path]:
    """Go's and Cargo's download stores, where builds fetch dependencies.

    Directories of executables on PATH (`~/go/bin`, `~/.cargo/bin`,
    `~/.local/bin`, the Homebrew prefix) and uv's data dir (pcode's own tool
    venv, managed Pythons) stay out: code planted there runs later outside
    the sandbox.
    """
    gopath = Path(_env_dir("GOPATH", "~/go"))
    cargo = Path(_env_dir("CARGO_HOME", "~/.cargo"))
    stores = [
        _env_dir("GOMODCACHE", str(gopath / "pkg" / "mod")),
        gopath / "pkg" / "sumdb",
        cargo / "registry",
        cargo / "git",
    ]
    return [real(store) for store in stores]


def _env_dir(name: str, default: str) -> str:
    """An env var naming a directory (the first, for a list like GOPATH).

    Ignored unless absolute, as Go does: a relative path would resolve
    against pcode's directory rather than the tool's.
    """
    value = os.path.expanduser(os.environ.get(name, "").split(os.pathsep)[0])
    return value if os.path.isabs(value) else default


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
    # Mach services a sandboxed command may not look up (macOS only).
    deny_services: list[str] = field(default_factory=list)
    # Directories whose Unix sockets a sandboxed command may not connect to:
    # session hosts take commands (shell mode among them) over theirs.
    deny_connect: list[Path] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        base: Iterable[Path],
        config: dict | None = None,
        grants: Iterable[Path] = (),
    ) -> Policy:
        from pcode import codex_login, remote_profile

        config = load_config() if config is None else config
        home = config_dir()
        deny = config.get("deny_read")
        if deny is None:
            deny = [
                *DEFAULT_DENY_READ,
                str(home / "credentials.json"),
                str(home / "mcp-credentials.json"),
                str(codex_login.credentials_path()),
            ]
        services: list[str] = []
        sockets: list[Path] = []
        protected = [real(home)]
        # A remote host's additions apply whatever sandbox.json says.
        if (profile := remote_profile.active()) is not None:
            from pcode.host_protocol import host_dir

            hosts = str(host_dir())
            deny = [*deny, *profile.deny_read, hosts, "~/Library/Keychains"]
            services = list(remote_profile.KEYCHAIN_SERVICES)
            sockets = [real(path) for path in (*profile.deny_read, hosts)]
            protected += profile_protected(base)
        return cls(
            write=_unique([*base, *(real(entry) for entry in config.get("write", [])), *grants]),
            protected=_unique(protected),
            deny_read=[_expand_pattern(str(entry)) for entry in deny],
            deny_services=services,
            deny_connect=sockets,
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
        if self.deny_connect:
            lines.append(
                _rule(
                    "deny network-outbound",
                    [
                        f"(remote unix-socket (subpath {_quote(path)}))"
                        for path in self.deny_connect
                    ],
                )
            )
        if self.deny_services:
            lines.append(
                _rule(
                    "deny mach-lookup",
                    [f"(global-name {_quote(name)})" for name in self.deny_services],
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
        # A tmpfs over a socket directory hides its sockets, so none can be reached.
        for path in self.deny_connect:
            if path.is_dir():
                args += ["--tmpfs", str(path)]
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
    if sys.platform.startswith("linux") and _bwrap():
        return "bwrap"
    return None


def _bwrap() -> str | None:
    """The distribution's bwrap first: Ubuntu 24.04+ lets only `/usr/bin/bwrap`
    create namespaces, and a Homebrew copy earlier on PATH would shadow it."""
    if os.access("/usr/bin/bwrap", os.X_OK):
        return "/usr/bin/bwrap"
    return shutil.which("bwrap")


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
        return [_bwrap() or "bwrap", *policy.bwrap_args([job_directory])]
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
