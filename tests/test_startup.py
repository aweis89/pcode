"""Editor availability is independent of backend and optional metadata readiness."""

import asyncio
import subprocess
import sys
import threading
from io import StringIO
from unittest.mock import patch

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.app import PreviewApp
from pcode.runtime import Message
from pcode.ui import create_prompt


def test_cli_constructor_and_toolbar_do_not_import_agent_stack():
    # A fresh interpreter is essential: the suite itself imports the backend.
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from io import StringIO
from rich.console import Console
from pcode.app import PreviewApp, main
app = PreviewApp(model='test:local', console=Console(file=StringIO()))
app.toolbar()
# Stop at the boundary where the CLI would start the interactive event loop.
PreviewApp.run = lambda self: None
sys.stdin.isatty = lambda: True
sys.stdout.isatty = lambda: True
sys.argv = ['pcode', '-m', 'test:local']
main()
for name in ('pcode.sessions', 'pcode.agent', 'pcode.live', 'pydantic_ai'):
    assert name not in sys.modules, name
""",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("outcome", ["success", "cancel", "failure", "quit", "restore", "commands"])
def test_editor_accepts_input_while_backend_starts(outcome):
    async def run():
        building = threading.Event()
        release = threading.Event()
        closed = threading.Event()
        metadata_started = asyncio.Event()
        metadata_cancelled = asyncio.Event()
        restore_started = asyncio.Event()
        restore_release = asyncio.Event()
        calls = []
        output = StringIO()

        class Runtime:
            session = None
            recovery_blocked = ""

            async def restore(self):
                restore_started.set()
                await restore_release.wait()

            async def refresh_context(self):
                metadata_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    metadata_cancelled.set()

            async def stream(self, text):
                calls.append(text)
                yield Message("response")

            def close(self):
                closed.set()

        def build():
            assert threading.current_thread() is not threading.main_thread()
            building.set()
            assert release.wait(8)
            if outcome == "failure":
                raise ValueError("api_key=sk-synthetic-startup-secret")
            return Runtime()

        app = PreviewApp(
            model="test:local", console=Console(file=output), resume=outcome == "restore"
        )
        app._create_runtime = build
        # Resume replay needs a real saved session; recovery ordering is what
        # this test exercises. Existing session tests cover transcript replay.
        app.replay = lambda: None
        with create_pipe_input() as pipe:
            session = None

            def prompt(*args, **kwargs):
                nonlocal session
                session = create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)
                return session

            async def wait_for(predicate):
                for _ in range(500):
                    if predicate():
                        return
                    await asyncio.sleep(0.01)
                raise AssertionError(f"Timed out: {output.getvalue()}")

            async def drive():
                try:
                    await wait_for(lambda: building.is_set())
                    assert session.app.is_running
                    if outcome == "commands":
                        pipe.send_text("/effort high\r/theme light\r")
                        await wait_for(lambda: app.transcript.theme == "light")
                        # The frontend command must not wait behind /effort.
                        assert not release.is_set()
                    pipe.send_text("early draft")
                    await wait_for(lambda: session.default_buffer.text == "early draft")
                    assert not calls
                    if outcome == "quit":
                        pipe.send_text("\x03/status\r/quit\r")
                        await wait_for(lambda: not session.app.is_running)
                        release.set()
                        return
                    pipe.send_text("\r")
                    await wait_for(lambda: app.activity.queued == 1)
                    if outcome == "cancel":
                        pipe.send_text("\x03")
                        await wait_for(lambda: app.activity.queued == 0)
                    release.set()
                    if outcome == "restore":
                        await restore_started.wait()
                        assert not calls
                        pipe.send_text("typing during recovery")
                        await wait_for(
                            lambda: session.default_buffer.text == "typing during recovery"
                        )
                        restore_release.set()
                    await wait_for(lambda: not app._startup_pending)
                    if outcome == "failure":
                        await wait_for(lambda: "Agent startup failed" in output.getvalue())
                        assert not calls
                        assert "sk-synthetic-startup-secret" not in output.getvalue()
                        assert not app.activity.queued_prompts
                        pipe.send_text("retry\r")
                        await wait_for(lambda: "restart pcode" in output.getvalue())
                    else:
                        await metadata_started.wait()
                        if outcome != "cancel":
                            await wait_for(lambda: calls == ["early draft"])
                            await wait_for(lambda: not app.activity.busy)
                        else:
                            assert not calls
                        # A hung optional metadata request does not own input.
                        pipe.send_text("\x03still editable")
                        await wait_for(lambda: session.default_buffer.text == "still editable")
                    pipe.send_text("\x03/quit\r")
                finally:
                    release.set()
                    restore_release.set()

            with patch("pcode.app.create_prompt", prompt):
                await asyncio.wait_for(asyncio.gather(app.run_async(), drive()), timeout=10)
        if outcome == "quit":
            assert closed.is_set(), "late-created runtimes must be cleaned up on exit"
        elif outcome != "failure":
            assert metadata_cancelled.is_set()

    asyncio.run(run())
