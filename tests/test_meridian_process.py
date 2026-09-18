"""Managed proxy ownership tests; no upstream requests or credentials."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pcode import meridian_process as mp


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.delenv("PCODE_MERIDIAN_BASE_URL", raising=False)
    monkeypatch.delenv("PCODE_MERIDIAN_MANAGED", raising=False)
    monkeypatch.setattr(mp, "_instance", None)


def test_external_and_default_never_start(monkeypatch):
    start = Mock()
    monkeypatch.setattr(mp.ManagedMeridian, "start", start)
    assert mp.managed_endpoint() is None
    monkeypatch.setenv("PCODE_MERIDIAN_MANAGED", "1")
    monkeypatch.setenv("PCODE_MERIDIAN_BASE_URL", "http://localhost:3456")
    assert mp.managed_endpoint() is None
    start.assert_not_called()


def test_single_owned_instance_and_no_restart(monkeypatch):
    instance = SimpleNamespace(
        base_url="http://localhost:1234", api_key="private", process=Mock(), close=Mock()
    )
    instance.process.poll.return_value = None
    start = Mock(return_value=instance)
    monkeypatch.setattr(mp.ManagedMeridian, "start", start)
    monkeypatch.setattr(mp.atexit, "register", Mock())
    monkeypatch.setenv("PCODE_MERIDIAN_MANAGED", "1")
    assert mp.managed_endpoint() == (instance.base_url, "private")
    assert mp.managed_endpoint() == (instance.base_url, "private")
    start.assert_called_once()
    instance.process.poll.return_value = 1
    with pytest.raises(ValueError, match="not replayed"):
        mp.managed_endpoint()
    start.assert_called_once()


def test_start_isolates_config_and_cleans_up(monkeypatch):
    monkeypatch.setattr(mp.shutil, "which", lambda _: "/bin/meridian")
    monkeypatch.setattr(mp.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout="1.71.1\n"))
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
        assert env["MERIDIAN_SESSION_DIR"] == str(root / "sessions")
        assert env["MERIDIAN_HOST"] == "127.0.0.1"
        assert "MERIDIAN_PROFILES" not in env
        assert "CLAUDE_PROXY_PLUGIN_DIR" not in env
        assert env["MERIDIAN_API_KEY"] == instance.api_key
        assert json.loads((root / "sdk-features.json").read_text()) == {
            "passthrough": {"thinkingPassthrough": True}
        }
    finally:
        instance.close()
    process.terminate.assert_called_once()
    assert not root.exists()
    instance.close()


def test_unsupported_version_does_not_spawn(monkeypatch):
    monkeypatch.setattr(mp.shutil, "which", lambda _: "/bin/meridian")
    monkeypatch.setattr(mp.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout="9.0"))
    spawn = Mock()
    monkeypatch.setattr(mp.subprocess, "Popen", spawn)
    with pytest.raises(ValueError, match="verified version"):
        mp.ManagedMeridian().start()
    spawn.assert_not_called()


@pytest.mark.parametrize(
    "health,features,error",
    [
        ({"status": "healthy", "version": "1.71.1"}, {"thinkingPassthrough": True}, None),
        ({"auth": {"loggedIn": False}}, {}, "not logged in"),
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
    "saved,override,external,expected",
    [
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

    instance = SimpleNamespace(
        base_url="http://localhost:1234", api_key="private", process=Mock(), close=Mock()
    )
    instance.process.poll.return_value = None
    start = Mock(return_value=instance)
    monkeypatch.setattr(mp.ManagedMeridian, "start", start)
    monkeypatch.setattr(mp.atexit, "register", Mock())
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

    assert "set meridian_managed on" in config_arguments()
    assert "set meridian_managed off" in config_arguments()
