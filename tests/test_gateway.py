"""`pcode.gateway`: driving session hosts with no terminal, as a remote control does."""

import asyncio
import os
import subprocess
from pathlib import Path

from test_remote_profile import Reader
from test_session_host import Script, attach, start_host, stop_host, until
from test_session_host import host_dir as host_dir

from pcode import gateway, remote_profile
from pcode.host_protocol import HostEntry, list_hosts, write_entry
from pcode.remote import stop_entry, wait_for_exit
from pcode.remote_profile import RemoteProfile
from pcode.sessions import SavedSession


def git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    (path / "a.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(path), "add", "a.txt"], check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-C", str(path)]
        + ["commit", "-q", "-m", "init"],
        check=True,
    )
    return path


def test_send_returns_the_reply_and_how_the_turn_ended(tmp_path, host_dir):
    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            result = await asyncio.wait_for(gateway.send(host.entry, "hello"), 10)
            assert (result.reply, result.outcome) == ("Echo: hello", gateway.COMPLETED)
            assert result.changes.worktree == str(tmp_path)
            status = gateway.status(host.entry)
            assert (status.state, status.turns, status.title) == ("idle", 1, "hello")
            # Detached: the host keeps running for everyone else.
            await until(lambda: not host.clients)
            assert not host.stopped.is_set()
        finally:
            await stop_host(host)
        assert gateway.status(host.entry).state == "stopped"

    asyncio.run(run())


def test_a_queued_message_gets_only_its_own_reply(tmp_path, host_dir):
    async def run():
        script = Script()
        host = await start_host("aaaa1111", tmp_path, script)
        try:
            terminal, _, _ = await attach(host)
            terminal.submit("hang first", "queue")
            await until(lambda: host.buffer)
            sending = asyncio.create_task(gateway.send(host.entry, "second"))
            await until(lambda: host.activity.queued_prompts == ["second"])
            script.release("hang first")
            result = await asyncio.wait_for(sending, 10)
            assert result.reply == "Echo: second"
            terminal.close()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_stop_cancels_the_turn_and_the_queue_but_keeps_the_host(tmp_path, host_dir):
    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            running = asyncio.create_task(gateway.send(host.entry, "hang on"))
            queued = asyncio.create_task(gateway.send(host.entry, "after"))
            await until(lambda: host.activity.queued_prompts == ["after"])
            await until(lambda: any(call[0] == "turn_event" for call in host.buffer))
            await gateway.stop(host.entry)
            first, second = await asyncio.wait_for(asyncio.gather(running, queued), 10)
            assert first.outcome == gateway.CANCELLED
            assert first.reply == "Started."
            assert second.outcome == gateway.CANCELLED and second.reply == ""
            assert not host.stopped.is_set()
            # And it takes the next message as usual.
            assert (await gateway.send(host.entry, "again")).outcome == gateway.COMPLETED
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_a_turn_that_hits_a_limit_says_so(tmp_path, host_dir):
    remote_profile.activate(RemoteProfile(max_requests=1))

    async def run():
        host = await start_host("aaaa1111", tmp_path, Reader())
        try:
            result = await asyncio.wait_for(gateway.send(host.entry, "look"), 10)
            assert result.outcome == gateway.LIMIT
            assert any(note.startswith(remote_profile.LIMIT_PREFIX) for note in result.notes)
        finally:
            await stop_host(host)

    try:
        asyncio.run(run())
    finally:
        remote_profile.activate(None)


def test_changes_cover_commits_edits_and_new_files_since_the_base(tmp_path):
    repo = git_repo(tmp_path / "repo")
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    (repo / "a.txt").write_text("two\n")
    (repo / "new.txt").write_text("x\n")
    summary = gateway.changes(repo, base)
    assert summary.branch == "main"
    assert "a.txt" in summary.diff_stat and summary.untracked == ["new.txt"]
    assert "New files:" in summary.describe()
    assert "No changes." in gateway.changes(git_repo(tmp_path / "clean")).describe()


def test_a_session_whose_host_idled_out_resumes_on_its_own_model(tmp_path, host_dir, monkeypatch):
    saved = SavedSession.create("openai:gpt-test", tmp_path, tmp_path / "sessions")
    saved.close()
    spawned = {}

    class Process:
        returncode = None

        def poll(self):
            return None

        def wait(self):
            return 0

    def spawn(**options):
        spawned.update(options)
        write_entry(HostEntry(id="bbbb2222", pid=os.getpid(), model="m", workspace="/"))
        (host_dir / "bbbb2222.sock").touch()
        return "bbbb2222", Process(), host_dir / "bbbb2222.log"

    monkeypatch.setattr(gateway, "spawn_host", spawn)
    profile = RemoteProfile()

    async def run():
        return await gateway.start_session(
            tmp_path, profile=profile, resume=saved.info.id, session_dir=tmp_path / "sessions"
        )

    entry = asyncio.run(run())
    assert entry.id == "bbbb2222"
    assert spawned["resume"] == saved.info.id and spawned["model"] == "openai:gpt-test"
    assert spawned["profile"] is profile


def test_a_real_remote_host_starts_in_its_own_kept_worktree(tmp_path, host_dir):
    """`python -m pcode.host` under a profile: a fresh worktree, left in place after."""
    repo = git_repo(tmp_path / "repo")

    async def run():
        entry = await asyncio.wait_for(
            gateway.start_session(repo, profile=RemoteProfile(), model="test"), 120
        )
        try:
            workspace = Path(entry.workspace)
            assert workspace != repo and workspace.parent == repo / ".worktrees"
            await until(lambda: gateway.status(entry).state == "idle", timeout=60)
            await gateway.stop(entry)  # Nothing running: a no-op.
            assert gateway.status(entry).state == "idle"
        finally:
            await stop_entry(entry)
            await wait_for_exit(entry.pid)
        assert not list_hosts()
        assert workspace.is_dir()
        return entry

    entry = asyncio.run(run())
    log = Path(entry.log).read_text()
    assert "serving" in log, log


def test_the_profile_lines_name_every_fixed_rule():
    text = "\n".join(RemoteProfile().describe())
    for word in ("Sandbox", "Worktree", "Environment", "MCP", "Per turn", "trusted"):
        assert word in text
