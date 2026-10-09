import json

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
    brew = "/opt/homebrew/Cellar/pcode/0.1.2/libexec/.venv"
    assert update_check.upgrade_command(brew).startswith("brew ")
    uv = "/home/me/.local/share/uv/tools/pcode"
    assert update_check.upgrade_command(uv) == "uv tool upgrade pcode"
    assert update_check.upgrade_command("/home/me/venv") == "pip install --upgrade pcode"


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


def test_failed_fetch_is_silent_and_not_cached(tmp_path):
    path = tmp_path / "latest.json"
    assert update_check.latest_version(path=path, fetch=lambda: None, now=5) is None
    assert not path.exists()


def test_check_respects_opt_out(monkeypatch):
    monkeypatch.setenv("PCODE_NO_UPDATE_CHECK", "1")
    monkeypatch.setattr(update_check, "installed_version", lambda: "0.0.1")
    monkeypatch.setattr(update_check, "latest_version", lambda: "9.9.9")
    assert update_check.check() is None
    monkeypatch.delenv("PCODE_NO_UPDATE_CHECK")
    assert update_check.check().startswith("pcode 9.9.9 is available")
