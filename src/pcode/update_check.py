"""Tell the user when a newer pcode release is on PyPI.

Startup must not wait on the network, so the answer is cached (a day after a
success, a few hours after a failure) and only refreshed on a daemon thread
that exit never waits for. Every failure is silent: being offline or holding a
corrupt cache file is not worth a warning.
"""

import asyncio
import json
import os
import re
import sys
import threading
import time
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

PYPI_URL = "https://pypi.org/pypi/pcode/json"
CACHE_SECONDS = 24 * 60 * 60
FAILURE_CACHE_SECONDS = 6 * 60 * 60
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
    """This install's version, or None when unknown or an editable checkout.

    A checkout sitting exactly on a release tag reports a clean version, but
    its upgrade is a `git pull`, not anything this notice could suggest.
    """
    try:
        dist = distribution("pcode")
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
    except PackageNotFoundError, ValueError:
        return None
    if direct.get("dir_info", {}).get("editable"):
        return None
    return dist.version


def _cached(path: Path, now: float) -> tuple[bool, str | None]:
    """(fresh, latest): a fresh entry may record a failed lookup as None."""
    try:
        data = json.loads(path.read_text())
        checked, latest = data["checked"], data["latest"]
    except OSError, ValueError, TypeError, KeyError:
        return False, None
    if not isinstance(checked, int | float) or not (latest is None or isinstance(latest, str)):
        return False, None
    window = CACHE_SECONDS if latest else FAILURE_CACHE_SECONDS
    # A future timestamp (clock change, copied state) would otherwise stay fresh forever.
    return 0 <= now - checked <= window, latest


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
    """The newest release on PyPI, from the cache or one short request."""
    path = path or cache_path()
    now = time.time() if now is None else now
    fresh, latest = _cached(path, now)
    if fresh:
        return latest
    latest = fetch()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"latest": latest, "checked": now}))
    except OSError:
        pass
    return latest


def upgrade_command(prefix: str | None = None, executable: str | None = None) -> str:
    """How this install upgrades, judged by where its environment lives."""
    parts = Path(prefix or sys.prefix).parts
    if "Cellar" in parts:
        return "brew update && brew upgrade cruxwell/pcode/pcode"
    if "tools" in parts and "uv" in parts:
        return "uv tool upgrade pcode"
    if "pipx" in parts:
        return "pipx upgrade pcode"
    # The pip first on PATH may belong to another environment.
    return f"{executable or sys.executable} -m pip install --upgrade pcode"


def upgrade_notice(installed: str | None, latest: str | None, **where) -> str | None:
    """The startup warning, or None when this install is current or a dev build."""
    current, newest = release_tuple(installed), release_tuple(latest)
    if current is None or newest is None or newest <= current:
        return None
    return (
        f"pcode {latest} is available (you have {installed}). "
        f"Upgrade with `{upgrade_command(**where)}`."
    )


def check() -> str | None:
    """Blocking. Skipped for dev and editable builds, and under PCODE_NO_UPDATE_CHECK."""
    if os.environ.get("PCODE_NO_UPDATE_CHECK", "").strip():
        return None
    installed = installed_version()
    if release_tuple(installed) is None:
        return None
    return upgrade_notice(installed, latest_version())


async def check_in_background(blocking=check) -> str | None:
    """`check()` on a daemon thread, so quitting mid-request never waits for it.

    `asyncio.to_thread` would not do: `asyncio.run` joins its executor on the
    way out, and the request's timeout does not cover DNS resolution.
    """
    loop = asyncio.get_running_loop()
    result: asyncio.Future[str | None] = loop.create_future()

    def deliver(notice: str | None) -> None:
        if not result.done():
            result.set_result(notice)

    def run() -> None:
        try:
            notice = blocking()
        except Exception:  # noqa: BLE001 - best effort, never a traceback.
            notice = None
        try:
            loop.call_soon_threadsafe(deliver, notice)
        except RuntimeError:  # The loop closed first: nobody is waiting.
            pass

    threading.Thread(target=run, name="pcode-update-check", daemon=True).start()
    return await result
