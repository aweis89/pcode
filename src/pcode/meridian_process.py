"""Process-owned Meridian instances (not a shared daemon)."""

import atexit
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx2

from pcode.preferences import load_preferences

# Oldest release whose configuration contract this module relies on
# (`sdk-features.json`, `/settings/api/features`, the MERIDIAN_* variables below).
# Newer releases start as long as they pass the same readiness checks.
MINIMUM_VERSION = "1.71.1"
MODES = ("auto", "on", "off")
WATCH_INTERVAL_SECONDS = 1.0
# A crash loop is not worth chasing: stop after this many restarts in the window.
MAX_RESTARTS = 3
RESTART_WINDOW_SECONDS = 300.0
_lock = threading.Lock()
_instance = None


def version_tuple(text: str | None) -> tuple[int, ...] | None:
    match = re.search(r"\d+(?:\.\d+)+", text or "")
    return tuple(int(part) for part in match.group().split(".")) if match else None


def supported(version: str | None) -> bool:
    found = version_tuple(version)
    return found is not None and found >= version_tuple(MINIMUM_VERSION)


def session_store_dir() -> Path:
    """Meridian's session store, kept across pcode runs so resumes stay warm.

    With a temporary store a restarted Meridian no longer recognises the
    conversation and replays its whole history as flattened text. Meridian locks
    the store, so concurrent pcode processes can share it.
    """
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return state / "pcode" / "meridian" / "sessions"


class ManagedMeridian:
    def __init__(self):
        self.process = None
        self.directory = None
        self.executable = None
        self.port = None
        self.base_url = ""
        self.api_key = secrets.token_urlsafe(32)
        self.restarts: list[float] = []
        self.failure: str | None = None
        self._lock = threading.RLock()
        self._stopped = threading.Event()
        self._watchdog = None

    def start(self):
        executable = shutil.which("meridian")
        if not executable:
            raise ValueError("Managed Meridian requires meridian on PATH; install it first.")
        try:
            version = subprocess.run(
                [executable, "--version"], capture_output=True, text=True, timeout=10, check=True
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            raise ValueError("Could not verify the installed Meridian version.") from None
        if not supported(version):
            raise ValueError(
                f"Managed Meridian needs {MINIMUM_VERSION} or newer "
                f"(found {version or 'unknown'}). Run `pcode --upgrade-meridian`."
            )
        self.executable = executable
        self.directory = tempfile.TemporaryDirectory(prefix="pcode-meridian-")
        root = Path(self.directory.name)
        (root / "sdk-features.json").write_text(
            json.dumps({"passthrough": {"thinkingPassthrough": True}})
        )
        (root / "plugins").mkdir()
        (root / "plugins.json").write_text("[]")
        session_store_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        # The port and key stay fixed across restarts, so clients built against
        # this instance keep working after the watchdog replaces the process.
        self.base_url = f"http://127.0.0.1:{self.port}"
        try:
            self._spawn()
        except BaseException:
            self.close()
            raise
        self._watchdog = threading.Thread(
            target=self._watch, name="pcode-meridian-watchdog", daemon=True
        )
        self._watchdog.start()
        return self

    def _spawn(self):
        root = Path(self.directory.name)
        # Do not inherit shared proxy routing, plugins, or administrative settings.
        # HOME and Claude credentials remain unchanged: Meridian owns upstream auth.
        env = {
            k: v for k, v in os.environ.items() if not k.startswith(("MERIDIAN_", "CLAUDE_PROXY_"))
        }
        env.update(
            {
                "MERIDIAN_HOST": "127.0.0.1",
                "MERIDIAN_PORT": str(self.port),
                "MERIDIAN_API_KEY": self.api_key,
                "MERIDIAN_CONFIG_DIR": str(root),
                "MERIDIAN_SESSION_DIR": str(session_store_dir()),
                "MERIDIAN_PLUGIN_DIR": str(root / "plugins"),
                "MERIDIAN_PLUGIN_CONFIG": str(root / "plugins.json"),
                "MERIDIAN_DESIGN_TOKEN_PATH": str(root / "design-token.json"),
                "MERIDIAN_NO_UPDATE_CHECK": "1",
                "MERIDIAN_TELEMETRY_PERSIST": "0",
                "MERIDIAN_PASSTHROUGH": "1",
            }
        )
        # Never let server output corrupt the terminal or expose credentials.
        self.process = subprocess.Popen(
            [self.executable],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self.wait_ready()
        except BaseException:
            self._stop_process()
            raise

    def wait_ready(self):
        deadline = time.monotonic() + 30
        with httpx2.Client(trust_env=False, timeout=2) as client:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise ValueError(
                        "Managed Meridian exited during startup (possibly a port conflict)."
                    )
                try:
                    headers = {"x-api-key": self.api_key}
                    health = client.get(self.base_url + "/health", headers=headers).json()
                    if health.get("auth", {}).get("loggedIn") is False:
                        raise ValueError(
                            "Meridian is not logged in. Run /login meridian, then retry."
                        )
                    if health.get("status") == "healthy" and supported(health.get("version")):
                        response = client.get(
                            self.base_url + "/settings/api/features", headers=headers
                        )
                        response.raise_for_status()
                        if (
                            response.json().get("passthrough", {}).get("thinkingPassthrough")
                            is not True
                        ):
                            raise ValueError(
                                "Managed Meridian did not enable thinking passthrough."
                            )
                        return
                except (httpx2.HTTPError, json.JSONDecodeError):
                    pass
                time.sleep(0.1)
        raise ValueError("Managed Meridian was not ready within 30 seconds; check Claude login.")

    def ensure_running(self):
        """Replace an exited process on the same port, within the restart budget."""
        with self._lock:
            if self.failure:
                raise ValueError(self.failure)
            if self._stopped.is_set():
                raise ValueError("Managed Meridian was stopped. Restart pcode.")
            if self.process is not None and self.process.poll() is None:
                return
            now = time.monotonic()
            self.restarts = [t for t in self.restarts if now - t < RESTART_WINDOW_SECONDS]
            if len(self.restarts) >= MAX_RESTARTS:
                self.failure = (
                    "Managed Meridian keeps exiting. Run `meridian` in a terminal to see why, "
                    "then restart pcode."
                )
                raise ValueError(self.failure)
            self.restarts.append(now)
            self._spawn()

    def _watch(self):
        # Requests in flight when the process died are not replayed; the
        # runtime's own retry, or the next prompt, reaches the replacement.
        while not self._stopped.wait(WATCH_INTERVAL_SECONDS):
            try:
                self.ensure_running()
            except ValueError:
                if self.failure:
                    return

    def _stop_process(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            self.process = None

    def close(self):
        self._stopped.set()
        with self._lock:
            self._stop_process()
            if self.directory is not None:
                self.directory.cleanup()
                self.directory = None


def managed_mode() -> str:
    """`on`, `off`, or `auto`; `PCODE_MERIDIAN_MANAGED` overrides the saved choice."""
    override = os.environ.get("PCODE_MERIDIAN_MANAGED", "").strip()
    if override:
        if override not in {"0", "1"}:
            raise ValueError("PCODE_MERIDIAN_MANAGED must be 0 or 1.")
        return "on" if override == "1" else "off"
    mode = load_preferences().get("meridian_managed", "auto")
    return mode if mode in MODES else "auto"


def external_proxy_running(base_url: str) -> bool:
    """True when something that looks like Meridian answers at `base_url`."""
    try:
        response = httpx2.get(base_url + "/health", timeout=1, trust_env=False)
    except httpx2.HTTPError:
        return False
    if response.status_code in (401, 403):
        return True  # A key-protected proxy still counts as the user's own.
    try:
        return "status" in response.json()
    except (ValueError, AttributeError, TypeError):
        return False


def managed_base_url() -> str | None:
    """The owned instance's URL, if this process started one."""
    instance = _instance
    return instance.base_url if instance is not None else None


def managed_endpoint():
    """Return private connection details, or None to use an external proxy.

    `auto` prefers a proxy already answering at the default address, then starts
    a private instance when `meridian` is installed.
    """
    if os.environ.get("PCODE_MERIDIAN_BASE_URL", "").strip():
        return None
    mode = managed_mode()
    if mode == "off":
        return None
    global _instance
    with _lock:
        if _instance is None:
            if mode == "auto":
                from pcode.meridian import DEFAULT_BASE_URL

                if shutil.which("meridian") is None or external_proxy_running(DEFAULT_BASE_URL):
                    return None
            _instance = ManagedMeridian().start()
            atexit.register(_instance.close)
        _instance.ensure_running()
        return _instance.base_url, _instance.api_key
