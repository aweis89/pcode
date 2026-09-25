"""Managed proxy ownership tests; no upstream requests or credentials."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx2
import pytest

from pcode import meridian_process as mp


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.delenv("PCODE_MERIDIAN_BASE_URL", raising=False)
    monkeypatch.delenv("PCODE_MERIDIAN_MANAGED", raising=False)
    monkeypatch.setattr(mp, "_instance", None)
    monkeypatch.setattr(mp.atexit, "register", Mock())


def fake_instance():
    instance = SimpleNamespace(
        base_url="http://localhost:1234", api_key="private", ensure_running=Mock(), close=Mock()
    )
    return instance


def test_external_url_and_off_never_start(monkeypatch):
    start = Mock()
    monkeypatch.setattr(mp.ManagedMeridian, "start", start)
    monkeypatch.setattr(mp.shutil, "which", lambda _: "/bin/meridian")
    monkeypatch.setenv("PCODE_MERIDIAN_MANAGED", "1")
    monkeypatch.setenv("PCODE_MERIDIAN_BASE_URL", "http://localhost:3456")
    assert mp.managed_endpoint() is None
    monkeypatch.delenv("PCODE_MERIDIAN_BASE_URL")
    monkeypatch.setenv("PCODE_MERIDIAN_MANAGED", "0")
    assert mp.managed_endpoint() is None
    start.assert_not_called()


def test_auto_prefers_a_running_proxy_then_an_installed_meridian(monkeypatch):
    start = Mock(return_value=fake_instance())
    monkeypatch.setattr(mp.ManagedMeridian, "start", start)
    running = Mock(return_value=True)
    monkeypatch.setattr(mp, "external_proxy_running", running)
    monkeypatch.setattr(mp.shutil, "which", lambda _: None)
    assert mp.managed_endpoint() is None  # Nothing installed: external mode explains itself.
    monkeypatch.setattr(mp.shutil, "which", lambda _: "/bin/meridian")
    assert mp.managed_endpoint() is None  # The user's own proxy wins.
    running.assert_called_with("http://127.0.0.1:3456")
    start.assert_not_called()
    running.return_value = False
    assert mp.managed_endpoint() == ("http://localhost:1234", "private")
    start.assert_called_once()


def test_single_owned_instance_is_checked_each_time(monkeypatch):
    instance = fake_instance()
    start = Mock(return_value=instance)
    monkeypatch.setattr(mp.ManagedMeridian, "start", start)
    monkeypatch.setenv("PCODE_MERIDIAN_MANAGED", "1")
    assert mp.managed_endpoint() == (instance.base_url, "private")
    assert mp.managed_endpoint() == (instance.base_url, "private")
    start.assert_called_once()
    assert instance.ensure_running.call_count == 2
    assert mp.managed_base_url() == instance.base_url
    instance.ensure_running.side_effect = ValueError("Managed Meridian keeps exiting.")
    with pytest.raises(ValueError, match="keeps exiting"):
        mp.managed_endpoint()


def test_restart_keeps_port_and_key_within_budget(monkeypatch):
    instance = mp.ManagedMeridian()
    instance.port, instance.base_url = 4321, "http://127.0.0.1:4321"
    key = instance.api_key
    spawned = []

    def spawn():
        spawned.append((instance.port, instance.api_key))
        instance.process = Mock(poll=Mock(return_value=None))

    monkeypatch.setattr(instance, "_spawn", spawn)
    instance.process = Mock(poll=Mock(return_value=None))
    instance.ensure_running()
    assert spawned == []  # A live process is left alone.
    for _ in range(mp.MAX_RESTARTS):
        instance.process.poll.return_value = 1
        instance.ensure_running()
    assert spawned == [(4321, key)] * mp.MAX_RESTARTS
    instance.process.poll.return_value = 1
    with pytest.raises(ValueError, match="keeps exiting"):
        instance.ensure_running()
    with pytest.raises(ValueError, match="keeps exiting"):
        instance.ensure_running()  # The failure sticks; no further attempts.
    assert len(spawned) == mp.MAX_RESTARTS


def test_restart_budget_refills_after_the_window(monkeypatch):
    instance = mp.ManagedMeridian()
    monkeypatch.setattr(instance, "_spawn", Mock())
    instance.process = Mock(poll=Mock(return_value=1))
    clock = Mock(return_value=0.0)
    monkeypatch.setattr(mp.time, "monotonic", clock)
    for _ in range(mp.MAX_RESTARTS):
        instance.ensure_running()
    clock.return_value = mp.RESTART_WINDOW_SECONDS + 1
    instance.ensure_running()
    assert instance._spawn.call_count == mp.MAX_RESTARTS + 1


def test_start_isolates_config_persists_sessions_and_cleans_up(monkeypatch, tmp_path):
    monkeypatch.setattr(mp.shutil, "which", lambda _: "/bin/meridian")
    monkeypatch.setattr(mp.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout="1.76.1\n"))
    process = Mock()
    process.poll.return_value = None
    spawn = Mock(return_value=process)
    monkeypatch.setattr(mp.subprocess, "Popen", spawn)
    monkeypatch.setenv("MERIDIAN_PORT", "3456")
    monkeypatch.setenv("MERIDIAN_PROFILES", "secret-shared-override")
    monkeypatch.setenv("CLAUDE_PROXY_PLUGIN_DIR", "/shared/plugins")
    monkeypatch.setattr(mp.ManagedMeridian, "wait_ready", lambda _: None)
    instance = mp.ManagedMeridian().start()
    root = mp.Path(instance.directory.name)
    try:
        env = spawn.call_args.kwargs["env"]
        assert env["MERIDIAN_CONFIG_DIR"] == str(root)
        # Sessions outlive the process so a resumed conversation stays warm.
        assert env["MERIDIAN_SESSION_DIR"] == str(tmp_path / "state/pcode/meridian/sessions")
        assert mp.session_store_dir().is_dir()
        assert env["MERIDIAN_HOST"] == "127.0.0.1"
        assert env["MERIDIAN_PORT"] == str(instance.port) != "3456"
        assert "MERIDIAN_PROFILES" not in env
        assert "CLAUDE_PROXY_PLUGIN_DIR" not in env
        assert env["MERIDIAN_API_KEY"] == instance.api_key
        assert json.loads((root / "sdk-features.json").read_text()) == {
            "passthrough": {"thinkingPassthrough": True}
        }
        assert instance._watchdog.is_alive()
    finally:
        instance.close()
    process.terminate.assert_called_once()
    assert not root.exists()
    assert mp.session_store_dir().is_dir()
    instance._watchdog.join(timeout=5)
    assert not instance._watchdog.is_alive()
    instance.close()


def test_start_shares_the_users_profiles_and_names_the_one_to_use(monkeypatch, tmp_path):
    user = tmp_path / "user-meridian"
    (user / "profiles" / "work").mkdir(parents=True)
    (user / "profiles.json").write_text(json.dumps([{"id": "personal"}, {"id": "work"}]))
    (user / "settings.json").write_text(json.dumps({"activeProfile": "work"}))
    monkeypatch.setenv("MERIDIAN_CONFIG_DIR", str(user))
    monkeypatch.setattr(mp.shutil, "which", lambda _: "/bin/meridian")
    monkeypatch.setattr(mp.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout="1.76.1"))
    spawn = Mock(return_value=Mock(poll=Mock(return_value=None)))
    monkeypatch.setattr(mp.subprocess, "Popen", spawn)
    monkeypatch.setattr(mp.ManagedMeridian, "wait_ready", lambda _: None)
    instance = mp.ManagedMeridian().start()
    try:
        root = mp.Path(instance.directory.name)
        env = spawn.call_args.kwargs["env"]
        assert env["MERIDIAN_CONFIG_DIR"] == str(root)  # Still private...
        assert env["MERIDIAN_DEFAULT_PROFILE"] == "work"
        # ...but profiles are linked, never copied, so tokens stay where they were.
        assert (root / "profiles.json").is_symlink()
        assert (root / "profiles.json").resolve() == (user / "profiles.json").resolve()
        assert (root / "profiles").resolve() == (user / "profiles").resolve()
        assert not (root / "settings.json").exists()
    finally:
        instance.close()
    assert (user / "profiles.json").exists()  # Cleanup removes links, not targets.


def test_default_profile_falls_back_to_the_first(monkeypatch, tmp_path):
    monkeypatch.setenv("MERIDIAN_CONFIG_DIR", str(tmp_path))
    assert mp.default_profile() is None
    (tmp_path / "profiles.json").write_text(json.dumps([{"id": "a"}, {"id": "b"}, "junk"]))
    assert mp.default_profile() == {"id": "a"}
    (tmp_path / "settings.json").write_text(json.dumps({"activeProfile": "gone"}))
    assert mp.default_profile() == {"id": "a"}
    assert mp.profile_dir({"id": "a"}) == str(tmp_path / "profiles" / "a")
    assert mp.profile_dir({"id": "a", "claudeConfigDir": "/x"}) == "/x"


def test_old_version_does_not_spawn(monkeypatch):
    monkeypatch.setattr(mp.shutil, "which", lambda _: "/bin/meridian")
    monkeypatch.setattr(mp.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout="1.70.9"))
    spawn = Mock()
    monkeypatch.setattr(mp.subprocess, "Popen", spawn)
    with pytest.raises(ValueError, match=r"1\.71\.1 or newer .*--upgrade-meridian"):
        mp.ManagedMeridian().start()
    spawn.assert_not_called()


@pytest.mark.parametrize(
    "version,expected",
    [("1.71.1", True), ("1.76.1", True), ("2.0", True), ("1.70.9", False), ("", False)],
)
def test_supported_versions(version, expected):
    assert mp.supported(version) is expected


@pytest.mark.parametrize(
    "health,features,error",
    [
        ({"status": "healthy", "version": "1.71.1"}, {"thinkingPassthrough": True}, None),
        ({"status": "healthy", "version": "1.76.1"}, {"thinkingPassthrough": True}, None),
        ({"auth": {"loggedIn": False}}, {}, "/login meridian"),
        ({"status": "healthy", "version": "1.71.1"}, {}, "thinking passthrough"),
    ],
)
def test_readiness(monkeypatch, health, features, error):
    client = Mock()
    client.get.side_effect = [
        Mock(json=lambda: health),
        Mock(json=lambda: {"passthrough": features}),
    ]
    context = Mock()
    context.__enter__ = Mock(return_value=client)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(mp.httpx2, "Client", lambda **kw: context)
    instance = mp.ManagedMeridian()
    instance.process = Mock()
    instance.process.poll.return_value = None
    if error:
        with pytest.raises(ValueError, match=error):
            instance.wait_ready()
    else:
        instance.wait_ready()
    assert client.get.call_args.kwargs["headers"] == {"x-api-key": instance.api_key}


def test_failed_readiness_stops_owned_process(monkeypatch):
    monkeypatch.setattr(mp.shutil, "which", lambda _: "/bin/meridian")
    monkeypatch.setattr(mp.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout="1.71.1"))
    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr(mp.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(mp.ManagedMeridian, "wait_ready", Mock(side_effect=ValueError("not ready")))
    instance = mp.ManagedMeridian()
    with pytest.raises(ValueError, match="not ready"):
        instance.start()
    process.terminate.assert_called_once()
    assert instance.directory is None
    assert instance.process is None
    assert instance._watchdog is None


def test_missing_executable(monkeypatch):
    monkeypatch.setattr(mp.shutil, "which", lambda _: None)
    with pytest.raises(ValueError, match="on PATH"):
        mp.ManagedMeridian().start()


def test_readiness_deadline(monkeypatch):
    monkeypatch.setattr(mp.time, "monotonic", Mock(side_effect=[0, 31]))
    instance = mp.ManagedMeridian()
    with pytest.raises(ValueError, match="30 seconds"):
        instance.wait_ready()


@pytest.mark.parametrize(
    "response,expected",
    [
        (httpx2.Response(200, json={"status": "healthy"}), True),
        (httpx2.Response(200, json={"status": "unhealthy", "auth": {"loggedIn": False}}), True),
        (httpx2.Response(401), True),
        (httpx2.Response(200, text="<html>not meridian</html>"), False),
        (httpx2.Response(404, json={"error": "nope"}), False),
        (httpx2.ConnectError("refused"), False),
    ],
)
def test_external_proxy_detection(monkeypatch, response, expected):
    def get(url, **kwargs):
        assert url == "http://127.0.0.1:3456/health"
        assert kwargs["trust_env"] is False
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(mp.httpx2, "get", get)
    assert mp.external_proxy_running("http://127.0.0.1:3456") is expected


@pytest.mark.parametrize(
    "saved,override,external,expected",
    [
        ("auto", None, False, True),
        ("auto", "0", False, False),
        ("on", None, False, True),
        ("off", None, False, False),
        ("on", "", False, True),
        ("on", "0", False, False),
        ("off", "1", False, True),
        ("on", None, True, False),
        ("on", "1", True, False),
    ],
)
def test_saved_preference_and_overrides(monkeypatch, saved, override, external, expected):
    from pcode.config import configure

    start = Mock(return_value=fake_instance())
    monkeypatch.setattr(mp.ManagedMeridian, "start", start)
    monkeypatch.setattr(mp.shutil, "which", lambda _: "/bin/meridian")
    monkeypatch.setattr(mp, "external_proxy_running", lambda _: False)
    configure(["set", "meridian_managed", saved])
    start.assert_not_called()  # Editing config itself has no lifecycle side effects.
    if override is not None:
        monkeypatch.setenv("PCODE_MERIDIAN_MANAGED", override)
    if external:
        monkeypatch.setenv("PCODE_MERIDIAN_BASE_URL", "http://localhost:3456")
    assert (mp.managed_endpoint() is not None) is expected
    assert start.call_count == int(expected)
    assert configure(["get", "meridian_managed"]) == saved


def test_invalid_environment_override(monkeypatch):
    monkeypatch.setenv("PCODE_MERIDIAN_MANAGED", "invalid")
    with pytest.raises(ValueError, match="must be 0 or 1"):
        mp.managed_endpoint()


def test_managed_setting_completion():
    from pcode.config import config_arguments

    for value in mp.MODES:
        assert f"set meridian_managed {value}" in config_arguments()
