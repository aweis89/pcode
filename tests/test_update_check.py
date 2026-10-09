import asyncio
import json
import threading
import time

import pytest

from pcode import update_check


def test_notice_only_when_a_newer_release_exists():
    assert update_check.upgrade_notice("0.1.2", "0.1.2") is None
    assert update_check.upgrade_notice("0.1.10", "0.1.9") is None
    notice = update_check.upgrade_notice("0.1.9", "0.1.10", prefix="/x/uv/tools/pcode")
    assert notice == (
        "pcode 0.1.10 is available (you have 0.1.9). Upgrade with `uv tool upgrade pcode`."
    )


def test_dev_and_unknown_versions_never_warn():
    assert update_check.upgrade_notice("0.1.3.dev4+gabc1234", "0.1.3") is None
    assert update_check.upgrade_notice(None, "0.1.3") is None
    assert update_check.upgrade_notice("0.1.2", None) is None
    assert update_check.upgrade_notice("0.1.2", "0.2.0rc1") is None


def test_upgrade_command_follows_the_install():
    command = update_check.upgrade_command
    assert command("/opt/homebrew/Cellar/pcode/0.1.2/libexec/.venv").startswith("brew ")
    assert command("/home/me/.local/share/uv/tools/pcode") == "uv tool upgrade pcode"
    assert command("/home/me/.local/share/pipx/venvs/pcode") == "pipx upgrade pcode"
    assert (
        command("/home/me/venv", "/home/me/venv/bin/python")
        == "/home/me/venv/bin/python -m pip install --upgrade pcode"
    )


def test_latest_version_uses_a_fresh_cache_without_fetching(tmp_path):
    path = tmp_path / "latest.json"
    path.write_text(json.dumps({"latest": "0.2.0", "checked": 1000}))

    def fetch():
        raise AssertionError("fetched despite a fresh cache")

    assert update_check.latest_version(path=path, fetch=fetch, now=1000 + 60) == "0.2.0"


def test_latest_version_refreshes_a_stale_cache(tmp_path):
    path = tmp_path / "state" / "latest.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"latest": "0.2.0", "checked": 0}))
    now = update_check.CACHE_SECONDS + 1

    assert update_check.latest_version(path=path, fetch=lambda: "0.3.0", now=now) == "0.3.0"
    assert json.loads(path.read_text()) == {"latest": "0.3.0", "checked": now}


def test_failed_fetch_is_remembered_for_a_shorter_window(tmp_path):
    path = tmp_path / "latest.json"
    calls = []

    def fetch():
        calls.append(1)

    assert update_check.latest_version(path=path, fetch=fetch, now=0) is None
    assert update_check.latest_version(path=path, fetch=fetch, now=60) is None
    assert len(calls) == 1
    later = update_check.FAILURE_CACHE_SECONDS + 1
    update_check.latest_version(path=path, fetch=fetch, now=later)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        json.dumps({"latest": "0.2.0", "checked": "oops"}),
        json.dumps({"latest": 5, "checked": 0}),
        json.dumps({"checked": 0}),
        # In the future: a clock change must not freeze the answer.
        json.dumps({"latest": "0.2.0", "checked": 10**12}),
    ],
)
def test_unusable_cache_is_refetched(tmp_path, content):
    path = tmp_path / "latest.json"
    path.write_text(content)
    assert update_check.latest_version(path=path, fetch=lambda: "0.9.0", now=0) == "0.9.0"


def test_check_respects_opt_out(monkeypatch):
    monkeypatch.setenv("PCODE_NO_UPDATE_CHECK", "1")
    monkeypatch.setattr(update_check, "installed_version", lambda: "0.0.1")
    monkeypatch.setattr(update_check, "latest_version", lambda: "9.9.9")
    assert update_check.check() is None
    monkeypatch.delenv("PCODE_NO_UPDATE_CHECK")
    assert update_check.check().startswith("pcode 9.9.9 is available")


def test_background_check_swallows_errors():
    def broken():
        raise TypeError("corrupt")

    assert asyncio.run(update_check.check_in_background(broken)) is None


def test_exit_does_not_wait_for_a_slow_lookup():
    release = threading.Event()

    def hung():
        release.wait(10)
        return "late"

    async def main():
        task = asyncio.create_task(update_check.check_in_background(hung))
        await asyncio.sleep(0)
        task.cancel()

    started = time.monotonic()
    asyncio.run(main())
    elapsed = time.monotonic() - started
    release.set()
    assert elapsed < 5
