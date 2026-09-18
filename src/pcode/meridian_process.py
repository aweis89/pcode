"""Opt-in, process-owned Meridian 1.71.1 instances (not a shared daemon)."""

import atexit
import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx2

SUPPORTED_VERSION = "1.71.1"
_lock = threading.Lock()
_instance = None


class ManagedMeridian:
    def __init__(self):
        self.process = None
        self.directory = None
        self.base_url = ""
        self.api_key = secrets.token_urlsafe(32)

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
        if version != SUPPORTED_VERSION:
            raise ValueError(
                f"Managed Meridian supports verified version {SUPPORTED_VERSION}; "
                "use an external PCODE_MERIDIAN_BASE_URL for other versions."
            )
        self.directory = tempfile.TemporaryDirectory(prefix="pcode-meridian-")
        root = Path(self.directory.name)
        (root / "sdk-features.json").write_text(
            json.dumps({"passthrough": {"thinkingPassthrough": True}})
        )
        (root / "plugins").mkdir()
        (root / "plugins.json").write_text("[]")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        # Do not inherit shared proxy routing, plugins, or administrative settings.
        # HOME and Claude credentials remain unchanged: Meridian owns upstream auth.
        env = {
            k: v for k, v in os.environ.items() if not k.startswith(("MERIDIAN_", "CLAUDE_PROXY_"))
        }
        env.update(
            {
                "MERIDIAN_HOST": "127.0.0.1",
                "MERIDIAN_PORT": str(port),
                "MERIDIAN_API_KEY": self.api_key,
                "MERIDIAN_CONFIG_DIR": str(root),
                "MERIDIAN_SESSION_DIR": str(root / "sessions"),
                "MERIDIAN_PLUGIN_DIR": str(root / "plugins"),
                "MERIDIAN_PLUGIN_CONFIG": str(root / "plugins.json"),
                "MERIDIAN_DESIGN_TOKEN_PATH": str(root / "design-token.json"),
                "MERIDIAN_NO_UPDATE_CHECK": "1",
                "MERIDIAN_TELEMETRY_PERSIST": "0",
                "MERIDIAN_PASSTHROUGH": "1",
            }
        )
        try:
            # Never let server output corrupt the terminal or expose credentials.
            self.process = subprocess.Popen(
                [executable],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.wait_ready()
        except BaseException:
            self.close()
            raise
        return self

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
                        raise ValueError("Meridian is not logged in. Run claude login, then retry.")
                    if (
                        health.get("status") == "healthy"
                        and health.get("version") == SUPPORTED_VERSION
                    ):
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

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            self.process = None
        if self.directory is not None:
            self.directory.cleanup()
            self.directory = None


def managed_endpoint():
    """Return private connection details, or None for externally managed mode."""
    if os.environ.get("PCODE_MERIDIAN_BASE_URL", "").strip():
        return None
    if os.environ.get("PCODE_MERIDIAN_MANAGED", "").strip() != "1":
        return None
    global _instance
    with _lock:
        if _instance is None:
            _instance = ManagedMeridian().start()
            atexit.register(_instance.close)
        elif _instance.process is None or _instance.process.poll() is not None:
            raise ValueError("Managed Meridian stopped. Restart pcode; requests are not replayed.")
        return _instance.base_url, _instance.api_key
