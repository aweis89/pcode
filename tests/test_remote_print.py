"""`pcode --attach HOST --print PROMPT`: one message or command for a running host, headless."""

import asyncio
import sys
from io import StringIO
from unittest.mock import patch

import pytest
from rich.console import Console
from test_session_host import Script, View, attach, start_host, stop_host, until
from test_session_host import host_dir as host_dir

from pcode.app import PreviewApp, main
from pcode.remote import RemoteController
from pcode.remote_print import print_to_host
from pcode.runtime import Message
from pcode.ui import Activity


class Printed:
    """What `--print` writes: the reply on `out`, everything else on `err`."""

    def __init__(self) -> None:
        self.out = StringIO()
        self.err = StringIO()
        self.app = PreviewApp(console=Console(file=self.err, width=120), theme="dark")

    async def send(self, host, prompt: str) -> bool:
        return await print_to_host(
            host.entry,
            prompt,
            transcript=self.app.transcript,
            present=self.app.present_events,
            stdout=self.out,
        )


def test_a_message_runs_in_the_host_and_its_reply_is_printed(tmp_path, host_dir):
    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            terminal, view, _ = await attach(host)
            printed = Printed()
            assert await asyncio.wait_for(printed.send(host, "hello"), 10)
            assert printed.out.getvalue() == "Echo: hello\n\n"
            # It is the host's conversation: an attached terminal saw the turn too.
            await until(lambda: Message("Echo: hello") in view.events())
            # Detached again; the host keeps running for everyone else.
            await until(lambda: len(host.clients) == 1)
            assert not host.stopped.is_set()
            terminal.close()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_a_message_waits_behind_a_running_turn_and_prints_only_its_own(tmp_path, host_dir):
    async def run():
        script = Script()
        host = await start_host("aaaa1111", tmp_path, script)
        try:
            terminal, view, _ = await attach(host)
            terminal.submit("hang first", "queue")
            await until(lambda: host.buffer)
            printed = Printed()
            sending = asyncio.create_task(printed.send(host, "second"))
            await until(lambda: host.activity.queued_prompts == ["second"])
            script.release("hang first")
            assert await asyncio.wait_for(sending, 10)
            # Queued, not steered into the other turn; that turn's text is not ours.
            assert printed.out.getvalue() == "Echo: second\n\n"
            terminal.close()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_a_message_cleared_from_the_queue_fails_instead_of_waiting(tmp_path, host_dir):
    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            terminal, _, _ = await attach(host)
            terminal.submit("hang forever", "queue")
            await until(lambda: host.buffer)
            printed = Printed()
            sending = asyncio.create_task(printed.send(host, "never runs"))
            await until(lambda: host.activity.queued_prompts == ["never runs"])
            terminal.cancel()
            assert not await asyncio.wait_for(sending, 10)
            assert printed.out.getvalue() == ""
            assert "dropped this message" in printed.err.getvalue()
            terminal.close()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_a_session_command_runs_in_the_host_and_returns_when_done(tmp_path, host_dir):
    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            printed = Printed()
            assert await asyncio.wait_for(printed.send(host, "/effort"), 10)
            assert printed.err.getvalue().startswith("Effort: ")
            assert printed.out.getvalue() == ""
            # A picker has nobody to show it to.
            printed = Printed()
            assert not await asyncio.wait_for(printed.send(host, "/model"), 10)
            assert "needs the interactive terminal" in printed.err.getvalue()
            # One the host lacks comes back to the sender, which says so and fails.
            printed = Printed()
            assert not await asyncio.wait_for(printed.send(host, "/no-such-command"), 10)
            assert "Unknown command /no-such-command" in printed.err.getvalue()
            assert printed.out.getvalue() == ""
            # Refused while the host works: a failure, not a quiet success.
            terminal, _, _ = await attach(host)
            terminal.submit("hang now", "queue")
            await until(lambda: host.buffer)
            printed = Printed()
            assert not await asyncio.wait_for(printed.send(host, "/compact"), 10)
            assert "unavailable while working" in printed.err.getvalue()
            terminal.cancel()
            terminal.close()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_compact_is_waited_for_not_just_started(tmp_path, host_dir):
    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            assert await asyncio.wait_for(Printed().send(host, "remember the parser"), 10)
            printed = Printed()
            assert await asyncio.wait_for(printed.send(host, "/compact"), 20)
            # The note saying how it ended arrives before pcode exits, not just
            # the one saying it started.
            started, ended = printed.err.getvalue().strip().splitlines()
            assert started.startswith("Compacting context")
            assert ended.startswith("Nothing to compact")
            assert host.controller.compact_task is None
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_run_returns_for_a_command_the_host_drops(tmp_path, host_dir):
    """A `run` caller has nothing else to wait on, so a skipped command must still report."""

    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            host.controller.startup_error = RuntimeError("no credentials")
            terminal, view, _ = await attach(host)
            await asyncio.wait_for(terminal.peer.request("run", "/effort"), 10)
            await until(lambda: view.count("warning"))
            assert "startup failed" in view.calls[view.names().index("warning")][1][0]
            terminal.close()
        finally:
            host.controller.startup_error = None
            await stop_host(host)

    asyncio.run(run())


def test_terminal_commands_are_refused_and_stop_ends_the_host(tmp_path, host_dir, monkeypatch):
    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            printed = Printed()
            assert not await printed.send(host, "/tree")
            assert "needs the interactive terminal" in printed.err.getvalue()
            assert not host.stopped.is_set()

            async def exited(pid, timeout=30.0):
                pass  # This host is the test process itself.

            monkeypatch.setattr("pcode.remote.wait_for_exit", exited)
            printed = Printed()
            assert await asyncio.wait_for(printed.send(host, "/stop"), 10)
            await until(host.stopped.is_set)
            assert "Stopped session host aaaa1111" in printed.err.getvalue()
            # Tidied by the host itself: nobody is here to ask.
            assert not host.keep_worktree
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_a_host_on_older_code_is_named_rather_than_hung_on(tmp_path, host_dir, monkeypatch):
    async def run():
        monkeypatch.setattr("pcode.host.HOST_CALLS", frozenset({"attach", "query", "stop"}))
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            from pcode.remote import HostError

            with pytest.raises(HostError, match="older pcode"):
                await asyncio.wait_for(Printed().send(host, "/effort"), 10)
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_attach_with_print_goes_to_the_host_without_an_editor(monkeypatch, tmp_path):
    monkeypatch.setenv("PCODE_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(sys, "argv", ["pcode", "--attach", "abc", "/stop", "--print"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    sent = []

    async def print_to_host(entry, prompt, **kwargs):
        sent.append((entry, prompt))
        return prompt == "/stop"

    with (
        patch("pcode.app._pick_host", return_value="entry") as pick,
        patch("pcode.remote_print.print_to_host", print_to_host),
        patch("pcode.app._run_hosted") as hosted,
    ):
        main()
        assert sent == [("entry", "/stop")]
        assert pick.call_args.args[0] == "abc"
        hosted.assert_not_called()
        monkeypatch.setattr(sys, "argv", ["pcode", "--attach", "--print", "go"])
        with pytest.raises(SystemExit) as raised:
            main()
        assert raised.value.code == 1
        assert pick.call_args.args[0] == ""


def test_a_remote_controller_detach_flushes_what_it_sent(tmp_path, host_dir):
    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            view = View()
            controller, welcome = await RemoteController.connect(host.socket, view, Activity())
            await controller.start(welcome)
            controller.submit("sent then detached", "queue")
            await controller.detach()
            await until(lambda: host.controller.runtime.session is not None)
            await until(lambda: not host.activity.busy)
        finally:
            await stop_host(host)

    asyncio.run(run())
