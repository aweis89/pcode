"""Signing Meridian in through Claude Code, and installing or upgrading Meridian.

Sign-in always runs the real `claude auth login`, so it completes through
Anthropic's own flow and pcode never sees the credential. Meridian can keep
several account profiles, each with its own Claude config directory; the login
goes to whichever one the Meridian that pcode uses will read.
"""

import asyncio
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx2

from pcode.auth import LoginError

PACKAGE = "@rynfar/meridian"
LOGIN_TIMEOUT_SECONDS = 300.0
# OSC (hyperlinks, titles) and CSI (colour, cursor) escapes in CLI output.
ESCAPES = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[ -/]*[@-~]")
REMOTE_VARIABLES = ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY", "NO_BROWSER")
# Printed without a newline, so whatever the CLI says next lands on the same line.
PASTE_PROMPT = re.compile(r"Paste code here if prompted >\s*")


@dataclass(frozen=True)
class LoginTarget:
    """Where `claude auth login` should write: a profile's config dir, or the default."""

    config_dir: str | None
    label: str
    oauth_token: bool = False


def meridian_config_dir() -> Path:
    return Path(os.environ.get("MERIDIAN_CONFIG_DIR") or Path.home() / ".config" / "meridian")


def profile_config_dir(profile_id: str) -> tuple[str, bool]:
    """(Claude config dir, uses an OAuth token) for a Meridian profile."""
    root = meridian_config_dir()
    try:
        profiles = json.loads((root / "profiles.json").read_text())
    except (OSError, ValueError):
        profiles = []
    for profile in profiles if isinstance(profiles, list) else []:
        if isinstance(profile, dict) and profile.get("id") == profile_id:
            token = bool(profile.get("oauthToken")) or profile.get("type") == "oauth-token"
            return str(profile.get("claudeConfigDir") or root / "profiles" / profile_id), token
    return str(root / "profiles" / profile_id), False


def active_profile(base_url: str) -> str | None:
    """The running proxy's active profile, or None when it has no profiles."""
    key = os.environ.get("PCODE_MERIDIAN_API_KEY", "").strip()
    try:
        response = httpx2.get(
            base_url + "/profiles/list",
            headers={"x-api-key": key} if key else {},
            timeout=2,
            trust_env=False,
        )
        data = response.json()
    except (httpx2.HTTPError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("profiles"):
        return None
    active = data.get("activeProfile")
    return active if isinstance(active, str) and active else None


def login_target() -> LoginTarget:
    """Match `managed_endpoint`: a managed instance reads Claude Code's default login.

    A managed instance runs with a private config directory, so it has no
    profiles; an external proxy reads its active profile's directory.
    """
    from pcode.meridian import DEFAULT_BASE_URL, meridian_base_url
    from pcode.meridian_process import external_proxy_running, managed_base_url, managed_mode

    default = LoginTarget(os.environ.get("CLAUDE_CONFIG_DIR") or None, "Claude Code's login")
    if managed_base_url() is not None:
        return default
    configured = os.environ.get("PCODE_MERIDIAN_BASE_URL", "").strip()
    mode = "off" if configured else managed_mode()
    base = meridian_base_url() if configured else DEFAULT_BASE_URL
    if mode == "on" or (mode == "auto" and not external_proxy_running(base)):
        return default
    profile = active_profile(base)
    if profile is None:
        return default
    config_dir, token = profile_config_dir(profile)
    return LoginTarget(config_dir, f"Meridian profile {profile}", oauth_token=token)


def remote_session() -> bool:
    return any(os.environ.get(name) for name in REMOTE_VARIABLES)


def claude_executable() -> str | None:
    return os.environ.get("MERIDIAN_CLAUDE_PATH") or shutil.which("claude")


def login_env(target: LoginTarget) -> dict[str, str]:
    env = dict(os.environ)
    if target.config_dir:
        env["CLAUDE_CONFIG_DIR"] = target.config_dir
    return env


def auth_status(executable: str, env: dict[str, str]) -> dict:
    """Only the non-identifying parts of `claude auth status`: no email or org."""
    try:
        result = subprocess.run(
            [executable, "auth", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        data = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: data[k] for k in ("loggedIn", "authMethod", "subscriptionType") if k in data}


def manual_login_command(target: LoginTarget) -> str:
    if target.config_dir:
        return f"CLAUDE_CONFIG_DIR={target.config_dir} claude auth login"
    return "claude auth login"


async def claude_login(notify, target: LoginTarget, timeout: float = LOGIN_TIMEOUT_SECONDS):
    """Run `claude auth login --claudeai` for `target` and return its auth status."""
    if target.oauth_token:
        raise LoginError(
            f"{target.label} signs in with a `claude setup-token` token, not a browser login. "
            "Replace it with `meridian profile add <name> --oauth-token`."
        )
    executable = claude_executable()
    if executable is None:
        raise LoginError(
            "Claude Code (`claude`) is not on PATH, and Meridian signs in through it. "
            "Install Claude Code, or set MERIDIAN_CLAUDE_PATH, then retry /login meridian."
        )
    if remote_session():
        raise LoginError(
            "This looks like a remote session, so the browser cannot finish signing in here. "
            f"Run `{manual_login_command(target)}` in a terminal on this machine, then retry."
        )
    env = login_env(target)
    process = await asyncio.create_subprocess_exec(
        executable,
        "auth",
        "login",
        "--claudeai",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )
    try:
        async with asyncio.timeout(timeout):
            async for raw in process.stdout:
                line = ESCAPES.sub("", raw.decode("utf-8", "replace"))
                line = PASTE_PROMPT.sub("", line).strip()
                if not line:
                    continue
                if line.startswith("If the browser didn't open"):
                    # That URL leads to a code to paste, which pcode cannot pass on.
                    notify(
                        "If no browser opened, press Ctrl+C and run "
                        f"`{manual_login_command(target)}` in a terminal instead."
                    )
                    continue
                notify(line)
            code = await process.wait()
    except TimeoutError:
        raise LoginError("Claude sign-in timed out. Run /login meridian to try again.") from None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if code != 0:
        raise LoginError(
            f"Claude sign-in did not complete (exit {code}). "
            f"Run `{manual_login_command(target)}` in a terminal to see why."
        )
    status = await asyncio.to_thread(auth_status, executable, env)
    if status.get("loggedIn") is False:
        raise LoginError("Claude Code still reports no login. Run /login meridian to try again.")
    return status


@dataclass(frozen=True)
class Install:
    """One npm-global Meridian installation."""

    root: Path
    npm: str

    @property
    def version(self) -> str | None:
        try:
            return json.loads((self.root / "package.json").read_text()).get("version")
        except (OSError, ValueError):
            return None


def _run(args: list[str], env=None) -> str | None:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=60, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def npm_env(npm: str) -> dict[str, str]:
    """Put the npm's own bin directory first so its node runs the install scripts."""
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(Path(npm).parent), env.get("PATH", "")])
    return env


def path_install() -> Install | None:
    """The Meridian that `meridian` on PATH runs, owned by the npm on PATH."""
    npm = shutil.which("npm")
    if npm is None:
        return None
    root = _run([npm, "root", "-g"])
    if not root:
        return None
    package = Path(root) / PACKAGE
    return Install(package, npm) if (package / "package.json").is_file() else None


def package_root(path: str) -> Path | None:
    """The `@rynfar/meridian` directory above any file inside the package."""
    for parent in Path(path).parents:
        if parent.name == "meridian" and parent.parent.name == "@rynfar":
            return parent
    return None


def proxy_install(base_url: str) -> Install | None:
    """The installation a running proxy was started from, via its /health report."""
    try:
        health = httpx2.get(base_url + "/health", timeout=2, trust_env=False).json()
        path = health["claudeExecutable"]["path"]
    except (httpx2.HTTPError, ValueError, KeyError, TypeError):
        return None
    root = package_root(path) if isinstance(path, str) else None
    if root is None:
        return None
    prefix = root.parent.parent.parent  # <prefix>/lib/node_modules/@rynfar/meridian
    if prefix.name == "lib":
        prefix = prefix.parent
    for npm in (prefix / "bin" / "npm", prefix / "npm"):
        if npm.is_file():
            return Install(root, str(npm))
    return None


def running_version(base_url: str) -> str | None:
    try:
        return httpx2.get(base_url + "/health", timeout=2, trust_env=False).json().get("version")
    except (httpx2.HTTPError, ValueError, AttributeError):
        return None


def launch_agent_label() -> str | None:
    """A macOS launchd agent that runs Meridian, if one is installed."""
    agents = Path.home() / "Library" / "LaunchAgents"
    for plist in sorted(agents.glob("*.plist")) if agents.is_dir() else ():
        try:
            data = plistlib.loads(plist.read_bytes())
        except Exception:
            continue
        program = " ".join(
            [str(data.get("Program", "")), *map(str, data.get("ProgramArguments", []))]
        )
        if "meridian" in program.lower() or "meridian" in str(data.get("Label", "")).lower():
            return data.get("Label")
    return None


def upgrade_meridian(out=None) -> int:
    """Install or upgrade every Meridian pcode uses; returns a process exit code."""
    from pcode.meridian import meridian_base_url

    out = out or sys.stdout

    def say(text: str) -> None:
        print(text, file=out, flush=True)

    try:
        base = meridian_base_url()
    except ValueError as error:
        say(str(error))
        return 2
    installs: dict[Path, Install] = {}
    for install in (path_install(), proxy_install(base)):
        if install is not None:
            installs.setdefault(install.root.resolve(), install)
    if not installs:
        npm = shutil.which("npm")
        if npm is None:
            say("Meridian is not installed and npm is not on PATH. Install Node.js, then retry.")
            return 1
        say(f"Meridian is not installed; installing {PACKAGE} with {npm}.")
        return subprocess.call([npm, "install", "-g", f"{PACKAGE}@latest"], env=npm_env(npm))
    failed = False
    for install in installs.values():
        before = install.version
        say(f"Upgrading {install.root} ({before or 'unknown version'}) with {install.npm}")
        code = subprocess.call(
            [install.npm, "install", "-g", f"{PACKAGE}@latest"], env=npm_env(install.npm)
        )
        after = install.version
        if code != 0:
            failed = True
            say(f"npm exited with {code}; {install.root} is still {after or 'unknown'}.")
        elif after == before:
            say(f"Already current: {after}.")
        else:
            say(f"Upgraded {before} -> {after}.")
    running = running_version(base)
    newest = max((i.version for i in installs.values() if i.version), default=None, key=_key)
    if running and newest and _key(running) < _key(newest):
        say(f"The proxy at {base} is still running {running}. Restart it to use {newest}.")
        label = launch_agent_label()
        if label:
            say(f"  launchctl kickstart -k gui/{os.getuid()}/{label}")
    say("pcode sessions that started their own Meridian pick up the new version on restart.")
    return 1 if failed else 0


def _key(version: str | None) -> tuple[int, ...]:
    from pcode.meridian_process import version_tuple

    return version_tuple(version) or ()
