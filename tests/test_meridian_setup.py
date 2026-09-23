"""Meridian sign-in and upgrade: fake `claude` and `npm`, no browser, network, or npm."""

import asyncio
import json
import os
import plistlib
import stat
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

import httpx2
import pytest

from pcode import meridian_process as mp
from pcode import meridian_setup as ms
from pcode.auth import LoginError


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    for name in (*ms.REMOTE_VARIABLES, "PCODE_MERIDIAN_BASE_URL", "PCODE_MERIDIAN_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("MERIDIAN_CLAUDE_PATH", raising=False)
    monkeypatch.setenv("MERIDIAN_CONFIG_DIR", str(tmp_path / "meridian"))
    monkeypatch.setattr(mp, "_instance", None)


def script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def write_profiles(tmp_path, profiles):
    root = tmp_path / "meridian"
    root.mkdir(exist_ok=True)
    (root / "profiles.json").write_text(json.dumps(profiles))


def serve_profiles(monkeypatch, active, ids):
    def get(url, **kwargs):
        assert url.endswith("/profiles/list")
        return httpx2.Response(
            200, json={"activeProfile": active, "profiles": [{"id": i} for i in ids]}
        )

    monkeypatch.setattr(ms.httpx2, "get", get)


# -- which login the Meridian in use reads -----------------------------------


def test_external_proxy_logs_in_its_active_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("PCODE_MERIDIAN_MANAGED", "0")
    write_profiles(
        tmp_path,
        [
            {"id": "personal", "claudeConfigDir": "/profiles/personal"},
            {"id": "sub", "claudeConfigDir": "/profiles/sub"},
        ],
    )
    serve_profiles(monkeypatch, "sub", ["personal", "sub"])
    target = ms.login_target()
    assert target == ms.LoginTarget("/profiles/sub", "Meridian profile sub")
    assert ms.manual_login_command(target) == "CLAUDE_CONFIG_DIR=/profiles/sub claude auth login"


def test_profile_without_a_recorded_dir_uses_meridians_default(monkeypatch, tmp_path):
    monkeypatch.setenv("PCODE_MERIDIAN_MANAGED", "0")
    write_profiles(tmp_path, [{"id": "work", "type": "oauth-token", "oauthToken": "x"}])
    serve_profiles(monkeypatch, "work", ["work"])
    target = ms.login_target()
    assert target.config_dir == str(tmp_path / "meridian" / "profiles" / "work")
    assert target.oauth_token


def test_proxy_without_profiles_uses_claude_codes_default(monkeypatch):
    monkeypatch.setenv("PCODE_MERIDIAN_MANAGED", "0")
    serve_profiles(monkeypatch, "default", [])
    assert ms.login_target() == ms.LoginTarget(None, "Claude Code's login")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/custom")
    assert ms.login_target().config_dir == "/custom"


def test_managed_instance_ignores_profiles(monkeypatch):
    lookup = Mock()
    monkeypatch.setattr(ms, "active_profile", lookup)
    monkeypatch.setenv("PCODE_MERIDIAN_MANAGED", "1")
    assert ms.login_target().label == "Claude Code's login"
    # auto with no proxy answering starts a managed instance, so the same applies.
    monkeypatch.setenv("PCODE_MERIDIAN_MANAGED", "")
    monkeypatch.setattr(mp, "external_proxy_running", lambda _: False)
    assert ms.login_target().label == "Claude Code's login"
    monkeypatch.setattr(mp, "_instance", Mock(base_url="http://127.0.0.1:9"))
    assert ms.login_target().label == "Claude Code's login"
    lookup.assert_not_called()


def test_configured_proxy_url_is_asked_for_its_profile(monkeypatch):
    monkeypatch.setenv("PCODE_MERIDIAN_BASE_URL", "http://127.0.0.1:4567/")
    monkeypatch.setenv("PCODE_MERIDIAN_MANAGED", "1")  # An explicit URL wins over managed.
    asked = []
    monkeypatch.setattr(ms, "active_profile", lambda base: asked.append(base))
    ms.login_target()
    assert asked == ["http://127.0.0.1:4567"]


# -- running `claude auth login` -----------------------------------------------

# Byte for byte what `claude auth login` printed without a TTY (URL shortened).
CLI_OUTPUT = (
    "Opening browser to sign in\u2026\n"
    "If the browser didn't open, visit: \x1b]8;;https://claude.com/x\x1b\\"
    "https://claude.com/x\x1b]8;;\x1b\\\n"
    "Paste code here if prompted > "
    "Login successful.\n"
)


def fake_claude(tmp_path, login_body=None, status=None):
    if login_body is None:
        (tmp_path / "login-output").write_text(CLI_OUTPUT)
        login_body = 'cat "$(dirname "$0")/login-output"\n'
    status = status or {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"}
    status = {**status, "email": "someone@example.com", "orgName": "Private Org"}
    return script(
        tmp_path / "claude",
        'echo "$CLAUDE_CONFIG_DIR|$*" >> "$(dirname "$0")/calls"\n'
        'if [ "$2" = status ]; then\n'
        f"  echo '{json.dumps(status)}'\n"
        "  exit 0\n"
        "fi\n" + login_body,
    )


def test_login_runs_claude_and_reports_only_non_identifying_status(monkeypatch, tmp_path):
    claude = fake_claude(tmp_path)
    monkeypatch.setenv("MERIDIAN_CLAUDE_PATH", str(claude))
    lines = []
    target = ms.LoginTarget(str(tmp_path / "profile"), "Meridian profile sub")
    status = asyncio.run(ms.claude_login(lines.append, target))
    assert status == {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"}
    calls = (tmp_path / "calls").read_text().splitlines()
    assert calls == [
        f"{tmp_path / 'profile'}|auth login --claudeai",
        f"{tmp_path / 'profile'}|auth status --json",
    ]
    assert lines[0] == "Opening browser to sign in…"
    assert "claude.com" not in "\n".join(lines)  # The paste-a-code URL is replaced...
    assert "CLAUDE_CONFIG_DIR=" in lines[1]  # ...with how to finish in a terminal.
    assert lines[-1] == "Login successful."
    assert not any("Paste code" in line or "\x1b" in line for line in lines)


@pytest.mark.parametrize(
    "body,status,message",
    [
        ("exit 3\n", None, "exit 3"),
        ("exit 0\n", {"loggedIn": False}, "still reports no login"),
    ],
)
def test_login_failures(monkeypatch, tmp_path, body, status, message):
    monkeypatch.setenv("MERIDIAN_CLAUDE_PATH", str(fake_claude(tmp_path, body, status)))
    with pytest.raises(LoginError, match=message):
        asyncio.run(ms.claude_login(lambda _: None, ms.LoginTarget(None, "Claude Code's login")))


def test_login_times_out_and_stops_the_cli(monkeypatch, tmp_path):
    monkeypatch.setenv("MERIDIAN_CLAUDE_PATH", str(fake_claude(tmp_path, "exec sleep 30\n")))
    with pytest.raises(LoginError, match="timed out"):
        asyncio.run(ms.claude_login(lambda _: None, ms.LoginTarget(None, "x"), timeout=0.5))


def test_cancelled_login_stops_the_cli(monkeypatch, tmp_path):
    pid_file = tmp_path / "pid"
    body = f'echo $$ > "{pid_file}"\nexec sleep 30\n'
    monkeypatch.setenv("MERIDIAN_CLAUDE_PATH", str(fake_claude(tmp_path, body)))

    async def run():
        task = asyncio.create_task(ms.claude_login(lambda _: None, ms.LoginTarget(None, "x")))
        while not pid_file.exists():
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_login_refuses_what_it_cannot_finish(monkeypatch, tmp_path):
    monkeypatch.setattr(ms.shutil, "which", lambda _: None)
    with pytest.raises(LoginError, match="not on PATH"):
        asyncio.run(ms.claude_login(lambda _: None, ms.LoginTarget(None, "x")))
    monkeypatch.setenv("MERIDIAN_CLAUDE_PATH", str(fake_claude(tmp_path)))
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 1 10.0.0.2 22")
    with pytest.raises(LoginError, match="remote session.*claude auth login"):
        asyncio.run(ms.claude_login(lambda _: None, ms.LoginTarget(None, "x")))
    token = ms.LoginTarget("/p", "Meridian profile ci", oauth_token=True)
    with pytest.raises(LoginError, match="setup-token"):
        asyncio.run(ms.claude_login(lambda _: None, token))
    assert not (tmp_path / "calls").exists()


def test_login_command_is_offered_and_dispatched():
    from pcode.app import PreviewApp

    app = PreviewApp()
    command = app.registry.find("/login")
    assert "meridian" in command.arguments
    app.login("meridian")
    assert app.login_requested == "meridian"
    app.login("nonsense")
    assert app.login_requested == "meridian"


# -- installing and upgrading ------------------------------------------------


def make_install(prefix: Path, version: str) -> Path:
    root = prefix / "lib" / "node_modules" / "@rynfar" / "meridian"
    root.mkdir(parents=True)
    (root / "package.json").write_text(json.dumps({"version": version}))
    (prefix / "bin").mkdir()
    return root


def fake_npm(prefix: Path, root: Path, new_version: str) -> Path:
    return script(
        prefix / "bin" / "npm",
        f'if [ "$1" = root ]; then echo "{root.parent.parent}"; exit 0; fi\n'
        f'echo "$PATH" | cut -d: -f1 > "{prefix}/path-head"\n'
        f"echo '{json.dumps({'version': new_version})}' > \"{root}/package.json\"\n",
    )


def test_package_root_from_the_proxys_bundled_claude():
    path = (
        "/opt/homebrew/lib/node_modules/@rynfar/meridian"
        "/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
    )
    assert ms.package_root(path) == Path("/opt/homebrew/lib/node_modules/@rynfar/meridian")
    assert ms.package_root("/usr/local/bin/claude") is None


def test_upgrade_covers_path_and_proxy_installs_and_says_how_to_restart(monkeypatch, tmp_path):
    shell = make_install(tmp_path / "shell", "1.72.0")
    fake_npm(tmp_path / "shell", shell, "1.76.1")
    brew = make_install(tmp_path / "brew", "1.72.0")
    fake_npm(tmp_path / "brew", brew, "1.76.1")
    monkeypatch.setattr(ms.shutil, "which", lambda name: str(tmp_path / "shell/bin/npm"))
    bundled = brew / "node_modules/@anthropic-ai/claude-code/bin/claude.exe"

    def get(url, **kwargs):
        return httpx2.Response(
            200, json={"version": "1.72.0", "claudeExecutable": {"path": str(bundled)}}
        )

    monkeypatch.setattr(ms.httpx2, "get", get)
    home = tmp_path / "home"
    (home / "Library/LaunchAgents").mkdir(parents=True)
    (home / "Library/LaunchAgents/local.meridian.plist").write_bytes(
        plistlib.dumps({"Label": "local.meridian", "ProgramArguments": ["/x/meridian/launch"]})
    )
    monkeypatch.setenv("HOME", str(home))
    out = StringIO()
    assert ms.upgrade_meridian(out) == 0
    text = out.getvalue()
    assert text.count("Upgraded 1.72.0 -> 1.76.1") == 2
    # Each install is upgraded by its own npm, with that npm's node first on PATH.
    assert (tmp_path / "shell/path-head").read_text().strip() == str(tmp_path / "shell/bin")
    assert (tmp_path / "brew/path-head").read_text().strip() == str(tmp_path / "brew/bin")
    assert "still running 1.72.0. Restart it to use 1.76.1" in text
    assert f"launchctl kickstart -k gui/{os.getuid()}/local.meridian" in text


def test_upgrade_reports_a_current_install_and_npm_failure(monkeypatch, tmp_path):
    root = make_install(tmp_path / "shell", "1.76.1")
    fake_npm(tmp_path / "shell", root, "1.76.1")
    monkeypatch.setattr(ms.shutil, "which", lambda name: str(tmp_path / "shell/bin/npm"))
    monkeypatch.setattr(ms, "proxy_install", lambda base: None)
    monkeypatch.setattr(ms, "running_version", lambda base: None)
    out = StringIO()
    assert ms.upgrade_meridian(out) == 0
    assert "Already current: 1.76.1." in out.getvalue()
    script(
        tmp_path / "shell/bin/npm",
        f'[ "$1" = root ] && echo "{root.parent.parent}" && exit 0\nexit 7\n',
    )
    out = StringIO()
    assert ms.upgrade_meridian(out) == 1
    assert "npm exited with 7" in out.getvalue()


def test_upgrade_installs_when_missing(monkeypatch, tmp_path):
    npm = script(
        tmp_path / "npm",
        f'[ "$1" = root ] && echo "{tmp_path}/none" && exit 0\necho "$*" > "{tmp_path}/args"\n',
    )
    monkeypatch.setattr(ms.shutil, "which", lambda name: str(npm))
    monkeypatch.setattr(ms, "proxy_install", lambda base: None)
    out = StringIO()
    assert ms.upgrade_meridian(out) == 0
    assert (tmp_path / "args").read_text().strip() == "install -g @rynfar/meridian@latest"
    assert "installing @rynfar/meridian" in out.getvalue()


def test_cli_flag_runs_the_upgrade_and_exits_with_its_status(monkeypatch):
    from pcode.app import main

    monkeypatch.setattr(sys, "argv", ["pcode", "--upgrade-meridian"])
    with patch("pcode.meridian_setup.upgrade_meridian", return_value=3) as upgrade:
        with patch("pcode.app.PreviewApp") as app:
            with pytest.raises(SystemExit) as exited:
                main()
    upgrade.assert_called_once()
    app.assert_not_called()
    assert exited.value.code == 3
