"""The fixed profile an unattended (remote) session host runs under."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from unittest.mock import patch

import pytest
from pydantic_ai.models.function import DeltaToolCall
from test_session_host import Script, attach, start_host, stop_host, until
from test_session_host import host_dir as host_dir

from pcode import remote_profile, sandbox
from pcode.agent import TurnLimits, create_coder
from pcode.ext import load_extensions, user_extension_dir
from pcode.isolated_delegation import WorkspaceSubAgents
from pcode.preferences import update_preferences
from pcode.remote import spawn_host
from pcode.remote_profile import LIMIT_PREFIX, RemoteProfile, TurnLimitReached


@pytest.fixture
def profile(tmp_path):
    """Activate a profile for this test, as `pcode.host` does at startup."""
    state = tmp_path / "listener-state"
    state.mkdir()
    active = RemoteProfile(deny_read=(str(state),), max_requests=3, max_tool_calls=2)
    remote_profile.activate(active)
    yield active
    remote_profile.activate(None)


def test_the_profile_round_trips_and_rejects_unknown_keys():
    profile = RemoteProfile(deny_read=("~/x",), mcp=True, turn_minutes=5, base="main")
    assert RemoteProfile.from_json(profile.to_json()) == profile
    with pytest.raises(ValueError, match="Unknown"):
        RemoteProfile.from_json(json.dumps({"sandbox": False}))
    assert any("MCP servers: default" in line for line in profile.describe())


def test_the_environment_is_an_allowlist():
    env = remote_profile.scrubbed_env(
        {
            "PATH": "/bin",
            "HOME": "/h",
            "LC_ALL": "C",
            "ANTHROPIC_API_KEY": "k",
            "GROQ_API_KEY": "g",
            "GITHUB_TOKEN": "secret",
            "GH_TOKEN": "secret",
            "NPM_TOKEN": "secret",
            "SOME_DIRENV_VALUE": "secret",
        }
    )
    assert env == {
        "ANTHROPIC_API_KEY": "k",
        "GROQ_API_KEY": "g",
        "HOME": "/h",
        "LC_ALL": "C",
        "PATH": "/bin",
    }


def test_a_remote_host_is_spawned_with_the_profile_and_a_scrubbed_environment(host_dir, tmp_path):
    profile = RemoteProfile(max_requests=7)
    with patch("pcode.remote.subprocess.Popen") as popen:
        with patch.dict(os.environ, {"GH_TOKEN": "secret", "OPENAI_API_KEY": "k"}):
            spawn_host(model="m", workspace=tmp_path, no_worktree=True, profile=profile)
    argv = popen.call_args.args[0]
    env = popen.call_args.kwargs["env"]
    assert RemoteProfile.from_json(argv[argv.index("--remote-profile") + 1]) == profile
    # The profile always makes a worktree, whatever the caller asked for.
    assert "--no-worktree" not in argv
    assert "GH_TOKEN" not in env and env["OPENAI_API_KEY"] == "k"
    assert env["PCODE_HOST_DIR"] == str(host_dir)


def test_the_policy_hides_listener_state_and_the_keychain(profile, tmp_path):
    rules = sandbox.Policy.build([tmp_path], config={"shell_sandbox": False, "deny_read": []})
    state = sandbox.real(profile.deny_read[0])
    # sandbox.json's own deny list is replaced by the user; the profile's is not.
    assert not rules.can_read(state / "state.json")
    assert not rules.can_read(sandbox.real(os.path.expanduser("~/Library/Keychains/x")))
    assert '(deny mach-lookup (global-name "com.apple.SecurityServer")' in (
        rules.seatbelt_profile()
    )
    remote_profile.activate(None)
    assert sandbox.Policy.build([tmp_path], config={}).deny_services == []


@pytest.mark.skipif(sandbox.backend() != "seatbelt", reason="needs macOS sandbox-exec")
def test_a_sandboxed_command_cannot_read_the_state_or_ask_the_keychain(profile, tmp_path):
    state = sandbox.real(profile.deny_read[0])
    (state / "state.json").write_text("secret")
    rules = sandbox.Policy.build([tmp_path], config={})
    prefix = sandbox.command_prefix(rules, tmp_path)
    read = subprocess.run([*prefix, "cat", str(state / "state.json")], capture_output=True)
    assert read.returncode != 0 and b"secret" not in read.stdout
    lookup = subprocess.run(
        [*prefix, "/usr/bin/security", "find-generic-password", "-s", "pcode-no-such-item"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    # Refused before the keychain is searched: unsandboxed, a missing item
    # says only "could not be found".
    assert lookup.returncode != 0
    assert "SecKeychainSearchCreateFromAttributes" in lookup.stderr


def test_the_bundled_sandbox_is_forced_on(profile, tmp_path):
    update_preferences({"extensions_off": "sandbox"})
    # A user extension of the same name would otherwise replace the bundled one.
    user = user_extension_dir()
    user.mkdir(parents=True)
    (user / "sandbox.py").write_text("def setup(pcode):\n    pass\n")
    loaded = load_extensions(tmp_path)
    (guard,) = [extension for extension in loaded.extensions if extension.name == "sandbox"]
    assert guard.scope == "bundled" and guard.disabled is None and guard.error is None
    remote_profile.activate(None)
    loaded = load_extensions(tmp_path)
    (guard,) = [extension for extension in loaded.extensions if extension.name == "sandbox"]
    assert guard.scope == "user"


def test_a_sandbox_that_fails_to_load_fails_startup(profile, tmp_path, monkeypatch):
    from pcode import ext

    real = ext.load_extension

    def broken(extension, *args, **kwargs):
        result = real(extension, *args, **kwargs)
        if extension.name == "sandbox":
            result.error = "boom"
        return result

    monkeypatch.setattr(ext, "load_extension", broken)
    with pytest.raises(RuntimeError, match="sandbox extension did not load"):
        load_extensions(tmp_path)


def test_limits_reach_the_agent_and_every_sub_agent(profile, tmp_path):
    coder = create_coder(tmp_path)
    top = [c for c in coder.capabilities if isinstance(c, TurnLimits)]
    (delegation,) = [c for c in coder.capabilities if isinstance(c, WorkspaceSubAgents)]
    shared = [c for c in delegation.shared_capabilities if isinstance(c, TurnLimits)]
    assert len(top) == 1 and len(shared) == 1
    remote_profile.activate(None)
    assert not [c for c in create_coder(tmp_path).capabilities if isinstance(c, TurnLimits)]


def test_the_budget_counts_requests_and_tool_calls_and_stays_spent(profile):
    budget = remote_profile.BUDGET
    budget.start()
    budget.charge_tool_call()
    budget.charge_tool_call()
    with pytest.raises(TurnLimitReached, match="2 tool calls"):
        budget.charge_tool_call()
    # Spent: a request after it fails as well, so a parent cannot carry on.
    with pytest.raises(TurnLimitReached):
        budget.charge_request()
    budget.start()
    for _ in range(3):
        budget.charge_request()
    with pytest.raises(TurnLimitReached, match="3 model requests"):
        budget.charge_request()


class Reader(Script):
    """Reads a file before every answer: two model requests and a tool call a turn."""

    async def model(self, messages, info):
        self.requests.append(messages)
        if len(self.requests) % 2:
            yield {1: DeltaToolCall(name="read_file", json_args='{"path": "sample.txt"}')}
        else:
            yield "Read it."


def test_a_turn_over_its_request_budget_ends_visibly(tmp_path, host_dir):
    remote_profile.activate(RemoteProfile(max_requests=1))

    async def run():
        host = await start_host("aaaa1111", tmp_path, Reader())
        try:
            terminal, view, _ = await attach(host)
            terminal.submit("look", "queue")
            await until(lambda: view.count("after_turn"))
            errors = [args[0] for name, args, _ in view.calls if name == "error"]
            assert any(error.startswith(LIMIT_PREFIX) for error in errors), errors
            assert host.entry.outcome == "failed"
            # The next turn has a fresh budget.
            terminal.submit("again", "queue")
            await until(lambda: view.count("after_turn") == 2)
            terminal.close()
        finally:
            await stop_host(host)

    try:
        asyncio.run(run())
    finally:
        remote_profile.activate(None)


def test_a_turn_past_its_wall_clock_is_stopped_with_a_warning(tmp_path, host_dir):
    remote_profile.activate(RemoteProfile(turn_minutes=0.002))

    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            terminal, view, _ = await attach(host)
            terminal.submit("hang forever", "queue")
            await until(lambda: view.count("after_turn"))
            warnings = [args[0] for name, args, _ in view.calls if name == "warning"]
            assert any(text.startswith(LIMIT_PREFIX) for text in warnings), warnings
            assert host.entry.outcome == "cancelled"
            assert host._turn_clock is None
            terminal.close()
        finally:
            await stop_host(host)

    try:
        asyncio.run(run())
    finally:
        remote_profile.activate(None)


def test_a_remote_host_always_gets_its_own_worktree(tmp_path):
    from pcode.app import _enter_worktree
    from pcode.worktree import WorktreeError

    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run([*git, "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    linked = tmp_path / "linked"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", str(linked)], check=True)
    # A local session inside a linked worktree stays there; a remote one never shares it.
    assert _enter_worktree(linked, True) == (linked, None)
    created, identity = _enter_worktree(linked, True, always=True, base="HEAD")
    assert identity and created != linked and created.is_dir()
    with pytest.raises(WorktreeError):
        _enter_worktree(tmp_path / "plain", True, always=True)


def test_the_host_entry_point_activates_the_profile(tmp_path):
    """`python -m pcode.host --remote-profile` parses what spawn_host writes."""
    from pcode.host import _parser

    args = _parser().parse_args(
        [
            "--id",
            "x",
            "--model",
            "m",
            "--workspace",
            str(tmp_path),
            "--remote-profile",
            RemoteProfile(mcp=True).to_json(),
        ]
    )
    assert RemoteProfile.from_json(args.remote_profile).mcp


# Ways out of the sandbox that the profile closes


def git_worktree(tmp_path, monkeypatch=None):
    """A repository and a session worktree on branch `pcode-s1`, as a remote host has.

    With `monkeypatch`, temp is moved away from pytest's tmp_path, which sits
    under $TMPDIR here and would otherwise be a write root of its own.
    """
    if monkeypatch is not None:
        elsewhere = tempfile.mkdtemp(prefix="pct-", dir="/tmp")
        monkeypatch.setattr(sandbox.tempfile, "gettempdir", lambda: elsewhere)
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run([*git, "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    tree = repo / ".worktrees" / "pcode-s1"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "pcode-s1", str(tree)], check=True
    )
    return sandbox.real(repo), sandbox.real(tree)


def worktree_roots(repo, tree):
    """`base_roots(tree)` minus the temp roots that hold `repo`.

    /tmp is always a write root, and on Linux pytest's tmp_path sits under it,
    which would make the whole shared checkout writable.
    """
    temp = {sandbox.real("/tmp"), sandbox.real("/var/tmp")}
    return [
        root
        for root in sandbox.base_roots(tree)
        if root not in temp or not repo.is_relative_to(root)
    ]


def test_a_remote_session_can_write_its_worktree_and_branch_but_not_git_config(
    profile, tmp_path, monkeypatch
):
    repo, tree = git_worktree(tmp_path, monkeypatch)
    rules = sandbox.Policy.build(worktree_roots(repo, tree), config={})
    git = repo / ".git"
    assert rules.can_write(tree / "src.py")
    assert rules.can_write(git / "objects" / "ab" / "cdef")
    assert rules.can_write(git / "refs" / "heads" / "pcode-s1")
    assert rules.can_write(git / "worktrees" / "pcode-s1" / "index")
    for path in (
        repo / "README.md",  # The shared checkout.
        git / "config",
        git / "refs" / "heads" / "main",
        tree / ".git",  # Where git finds the repository.
        git / "worktrees" / "pcode-s1" / "commondir",
        git / "worktrees" / "pcode-s1" / "config.worktree",
    ):
        assert not rules.can_write(path), path
    remote_profile.activate(None)
    assert sandbox.Policy.build(worktree_roots(repo, tree), config={}).can_write(git / "config")


@pytest.mark.skipif(sandbox.backend() != "seatbelt", reason="needs macOS sandbox-exec")
def test_a_sandboxed_command_can_commit_in_its_worktree_but_not_touch_the_config(
    profile, tmp_path, monkeypatch
):
    repo, tree = git_worktree(tmp_path, monkeypatch)
    rules = sandbox.Policy.build(worktree_roots(repo, tree), config={})
    prefix = sandbox.command_prefix(rules, tmp_path / "job")
    script = (
        "echo x > f.txt && git add f.txt && "
        "git -c user.email=t@example.com -c user.name=t commit -qm work && echo committed; "
        "git config core.fsmonitor 'touch pwned' || echo refused"
    )
    result = subprocess.run(
        [*prefix, "sh", "-c", script], cwd=tree, capture_output=True, text=True, timeout=60
    )
    assert "committed" in result.stdout, result.stderr
    assert "refused" in result.stdout
    assert "fsmonitor" not in (repo / ".git" / "config").read_text()


@pytest.mark.skipif(sandbox.backend() != "seatbelt", reason="needs macOS sandbox-exec")
def test_a_sandboxed_command_cannot_reach_a_session_host_socket(profile, host_dir):
    import socket

    path = host_dir / "aaaa1111.sock"
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen()
    try:
        rules = sandbox.Policy.build([host_dir.parent], config={})
        prefix = sandbox.command_prefix(rules, host_dir.parent)
        code = f"import socket; socket.socket(socket.AF_UNIX).connect({str(path)!r})"
        result = subprocess.run(
            [*prefix, sys.executable, "-c", code], capture_output=True, text=True, timeout=30
        )
        assert result.returncode != 0 and "not permitted" in result.stderr
    finally:
        server.close()


def test_the_listeners_git_calls_ignore_config_that_runs_commands(tmp_path):
    from pcode.gateway import changes

    repo, tree = git_worktree(tmp_path)
    marker = tmp_path / "pwned"
    (tree / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(tree), "add", "f.txt"], check=True)
    for key in ("core.fsmonitor", "diff.external"):
        subprocess.run(["git", "-C", str(repo), "config", key, f"touch {marker}"], check=True)
    summary = changes(tree)
    assert "f.txt" in summary.diff_stat and summary.branch == "pcode-s1"
    assert not marker.exists()


def test_a_remote_host_refuses_shell_mode(tmp_path, host_dir):
    remote_profile.activate(RemoteProfile())

    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            terminal, _, _ = await attach(host)
            with pytest.raises(Exception, match="Shell mode is off"):
                await terminal.peer.request("submit", "!echo hi", "shell")
            terminal.close()
        finally:
            await stop_host(host)

    try:
        asyncio.run(run())
    finally:
        remote_profile.activate(None)


def test_a_remote_session_whose_worktree_is_gone_resumes_in_a_new_one(tmp_path):
    from types import SimpleNamespace

    from pcode.host import remote_resume_workspace

    repo, tree = git_worktree(tmp_path)
    profile = RemoteProfile()
    assert remote_resume_workspace(SimpleNamespace(workspace=str(tree)), repo, profile) == tree
    gone = SimpleNamespace(workspace=str(repo / ".worktrees" / "removed"))
    created = remote_resume_workspace(gone, repo, profile)
    assert created != repo and created.parent == repo / ".worktrees" and created.is_dir()
