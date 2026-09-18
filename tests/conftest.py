import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

# Private tmux servers are parented to init, so a pytest that dies without
# running fixture teardown (SIGKILL, a timeout, an abandoned CI runner) strands
# the server plus its ~170MB Python child forever. Sweep the previous run's
# corpses before starting a new one.
TMUX_SOCKET_PREFIXES = ("pcode-test-", "pcode-bench-")
# A single tmux test lives for seconds; this is only long enough to never touch
# a concurrent run's sockets.
STALE_TMUX_SERVER_SECONDS = 15 * 60


def tmux_socket_dir() -> Path:
    """Where tmux keeps per-user sockets (`-L` names resolve inside this)."""
    return Path(os.environ.get("TMUX_TMPDIR", "/tmp")) / f"tmux-{os.getuid()}"


def sweep_tmux_servers(*, max_age_seconds: float) -> int:
    """Kill leaked test servers, and unlink sockets with no server behind them.

    Only sockets older than ``max_age_seconds`` are killed, so a second pytest
    process running at the same time keeps its own servers.
    """
    directory = tmux_socket_dir()
    if shutil.which("tmux") is None or not directory.is_dir():
        return 0
    reaped = 0
    now = time.time()
    for socket in directory.iterdir():
        if not socket.name.startswith(TMUX_SOCKET_PREFIXES):
            continue
        base = ["tmux", "-L", socket.name, "-f", "/dev/null"]
        alive = subprocess.run([*base, "list-sessions"], capture_output=True).returncode == 0
        try:
            expired = now - socket.stat().st_mtime > max_age_seconds
        except OSError:
            continue
        if alive and not expired:
            continue
        if alive:
            subprocess.run([*base, "kill-server"], capture_output=True)
            reaped += 1
        socket.unlink(missing_ok=True)
    return reaped


@pytest.fixture(scope="session", autouse=True)
def reap_leaked_tmux_servers():
    """Bound the damage from any earlier run that never reached its teardown."""
    sweep_tmux_servers(max_age_seconds=STALE_TMUX_SERVER_SECONDS)
    yield
    # This run's own sockets are gone by now; clear the dead files they leave.
    sweep_tmux_servers(max_age_seconds=float("inf"))


@pytest.fixture(autouse=True)
def isolated_preferences(monkeypatch, tmp_path):
    """Tests must neither consume nor overwrite the user's saved defaults."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("PCODE_MCP_CONFIG", raising=False)


@pytest.fixture(autouse=True)
def isolated_context_catalog(monkeypatch, tmp_path):
    """Never fetch real metadata or read user caches in ordinary unit tests.

    Adapter tests construct their own ContextCatalog with mocked transports.
    """
    from unittest.mock import AsyncMock

    from pcode import model_metadata

    catalog = model_metadata.ContextCatalog(tmp_path / "metadata.json")
    monkeypatch.setattr(catalog, "refresh", AsyncMock())
    monkeypatch.setattr(model_metadata, "catalog", catalog)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("PCODE_CONTEXT_WINDOW", raising=False)
