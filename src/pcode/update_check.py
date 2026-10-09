"""Tell the user when a newer pcode release is on PyPI.

Startup must not wait on the network, so the latest version is cached for a
day and only refreshed off the event loop. A failed lookup is silent: being
offline is not worth a warning.
"""

import json
import os
import re
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

PYPI_URL = "https://pypi.org/pypi/pcode/json"
CACHE_SECONDS = 24 * 60 * 60
TIMEOUT_SECONDS = 3
_RELEASE = re.compile(r"\d+(\.\d+)*")


def cache_path() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return state / "pcode" / "latest-version.json"


def release_tuple(text: str | None) -> tuple[int, ...] | None:
    """A plain release such as 0.1.2; None for dev, pre-release and local builds."""
    if not text or not _RELEASE.fullmatch(text):
        return None
    return tuple(int(part) for part in text.split("."))


def installed_version() -> str | None:
    try:
        return version("pcode")
    except PackageNotFoundError:
        return None


def _cached(path: Path, now: float) -> str | None:
    try:
        data = json.loads(path.read_text())
    except OSError, ValueError:
        return None
    if not isinstance(data, dict) or now - data.get("checked", 0) > CACHE_SECONDS:
        return None
    latest = data.get("latest")
    return latest if isinstance(latest, str) else None


def _fetch() -> str | None:
    import httpx2

    try:
        response = httpx2.get(PYPI_URL, timeout=TIMEOUT_SECONDS, follow_redirects=True)
        response.raise_for_status()
        latest = response.json()["info"]["version"]
    except Exception:  # noqa: BLE001 - any failure means "don't know".
        return None
    return latest if isinstance(latest, str) else None


def latest_version(
    *, path: Path | None = None, fetch=_fetch, now: float | None = None
) -> str | None:
    """The newest release on PyPI, from a day-old cache or one short request."""
    path = path or cache_path()
    now = time.time() if now is None else now
    if (cached := _cached(path, now)) is not None:
        return cached
    latest = fetch()
    if latest is None:
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"latest": latest, "checked": now}))
    except OSError:
        pass
    return latest


def upgrade_command(prefix: str | None = None) -> str:
    """How this install upgrades, judged by where its environment lives."""
    parts = Path(prefix or sys.prefix).parts
    if "Cellar" in parts:
        return "brew update && brew upgrade cruxwell/pcode/pcode"
    if "tools" in parts and "uv" in parts:
        return "uv tool upgrade pcode"
    return "pip install --upgrade pcode"


def upgrade_notice(installed: str | None, latest: str | None, *, prefix=None) -> str | None:
    """The startup warning, or None when this install is current or a dev build."""
    current, newest = release_tuple(installed), release_tuple(latest)
    if current is None or newest is None or newest <= current:
        return None
    return (
        f"pcode {latest} is available (you have {installed}). "
        f"Upgrade with `{upgrade_command(prefix)}`."
    )


def check() -> str | None:
    """Blocking: call from a thread. Skipped for dev builds and PCODE_NO_UPDATE_CHECK."""
    if os.environ.get("PCODE_NO_UPDATE_CHECK", "").strip():
        return None
    installed = installed_version()
    if release_tuple(installed) is None:
        return None
    return upgrade_notice(installed, latest_version())
