"""`/login antigravity`: a fake `agy` CLI and `SecretPrompt`, no browser or network."""

import asyncio
import os
import stat
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.layout.processors import PasswordProcessor
from prompt_toolkit.output.defaults import DummyOutput
from rich.console import Console

from pcode import antigravity_login as al
from pcode.auth import LoginError
from pcode.secret_ui import SecretPrompt


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY", "NO_BROWSER"):
        monkeypatch.delenv(name, raising=False)


def script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


CLI_PROMPT = (
    "Open this link in a browser to log in with Google:\n\n"
    "  https://accounts.google.com/o/oauth2/auth?client_id=test&state=abc\n\n"
    "Then paste the authorization code, or the full URL of the page you land on, and press Enter:\n"
)


def fake_agy(tmp_path: Path, body: str | None = None) -> str:
    if body is None:
        (tmp_path / "prompt.txt").write_text(CLI_PROMPT)
        body = (
            'echo "[$GEMINI_API_KEY][$GOOGLE_CLOUD_PROJECT]|$*" >> "$(dirname "$0")/calls"\n'
            'cat "$(dirname "$0")/prompt.txt"\n'
            "IFS= read -r code\n"
            'echo "$code" > "$(dirname "$0")/code"\n'
            'echo "Logged in."\n'
        )
    return str(script(tmp_path / "agy", body))


def test_login_opens_browser_scrubs_env_and_forwards_code(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-key")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "synthetic-project")
    opened = []
    monkeypatch.setattr(al, "_open_browser", opened.append)

    notes = []

    async def read_code():
        return "  secret-auth-code  "

    asyncio.run(al.antigravity_login(notes.append, read_code, fake_agy(tmp_path)))

    assert opened == ["https://accounts.google.com/o/oauth2/auth?client_id=test&state=abc"]
    assert notes[0] == "https://accounts.google.com/o/oauth2/auth?client_id=test&state=abc"
    assert "Paste the authorization code" in notes[1]
    assert "Completing Antigravity sign-in" in notes[2]
    # The pasted code is never echoed back to the transcript.
    assert not any("secret-auth-code" in line for line in notes)
    assert (tmp_path / "code").read_text().strip() == "secret-auth-code"
    assert (tmp_path / "calls").read_text().splitlines() == ["[][]|acp --login"]


def test_login_handles_prompt_without_trailing_newline(monkeypatch, tmp_path):
    monkeypatch.setattr(al, "_open_browser", lambda _: None)
    agy = fake_agy(
        tmp_path,
        'printf "Open: https://accounts.google.com/o/oauth2/auth?state=1\\n"\n'
        'printf "Paste code here: "\n'
        "IFS= read -r code\n"
        'echo "$code" > "$(dirname "$0")/code"\n',
    )

    async def read_code():
        return "no-newline-code"

    asyncio.run(al.antigravity_login(lambda _: None, read_code, agy, timeout=3.0))
    assert (tmp_path / "code").read_text().strip() == "no-newline-code"


def test_login_skips_browser_in_remote_session(monkeypatch, tmp_path):
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 1 10.0.0.2 22")
    opened = []
    monkeypatch.setattr(al, "_open_browser", opened.append)
    notes = []

    async def read_code():
        return "remote-code"

    asyncio.run(al.antigravity_login(notes.append, read_code, fake_agy(tmp_path)))

    assert opened == []
    assert notes[0].startswith("https://accounts.google.com/")
    assert (tmp_path / "code").read_text().strip() == "remote-code"


def test_login_reports_cli_error_and_redacts_callback_code(monkeypatch, tmp_path):
    monkeypatch.setattr(al, "_open_browser", lambda _: None)
    (tmp_path / "prompt.txt").write_text(CLI_PROMPT)
    agy = fake_agy(
        tmp_path,
        'cat "$(dirname "$0")/prompt.txt"\n'
        "IFS= read -r _code\n"
        'echo "couldn\'t log in: invalid_grant"\n'
        "exit 1\n",
    )

    async def read_code():
        return "bad-code"

    with pytest.raises(LoginError, match="exit 1.*invalid_grant"):
        asyncio.run(al.antigravity_login(lambda _: None, read_code, agy))

    # Even if the CLI echoes only the extracted `code` query param from a pasted
    # callback URL, it must never leak into LoginError.
    leaky = fake_agy(
        tmp_path,
        'cat "$(dirname "$0")/prompt.txt"\n'
        "IFS= read -r _code\n"
        'echo "token exchange failed for 4/0AsecretCodeXYZ"\n'
        "exit 1\n",
    )

    async def read_callback_url():
        return "https://antigravity.google/oauth-callback?state=xyz&code=4%2F0AsecretCodeXYZ"

    with pytest.raises(LoginError) as caught:
        asyncio.run(al.antigravity_login(lambda _: None, read_callback_url, leaky))
    assert "secretCodeXYZ" not in str(caught.value)
    assert "exit 1" in str(caught.value)


def test_login_cancel_from_read_code_none_kills_process(monkeypatch, tmp_path):
    monkeypatch.setattr(al, "_open_browser", lambda _: None)
    pid_file = tmp_path / "pid"
    (tmp_path / "prompt.txt").write_text(CLI_PROMPT)
    agy = fake_agy(
        tmp_path,
        f'echo $$ > "{pid_file}"\ncat "$(dirname "$0")/prompt.txt"\nexec sleep 30\n',
    )

    async def read_code():
        return None

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(al.antigravity_login(lambda _: None, read_code, agy))
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_login_requires_agy_binary(monkeypatch):
    monkeypatch.setattr(al.shutil, "which", lambda _: None)

    async def read_code():
        return ""

    with pytest.raises(LoginError, match=r"not found\. Install it, then retry /login antigravity"):
        asyncio.run(al.antigravity_login(lambda _: None, read_code))


def test_login_times_out_and_kills_process(monkeypatch, tmp_path):
    monkeypatch.setattr(al, "_open_browser", lambda _: None)
    pid_file = tmp_path / "pid"
    agy = fake_agy(tmp_path, f'echo $$ > "{pid_file}"\nexec sleep 30\n')

    async def read_code():
        await asyncio.sleep(30)
        return ""

    with pytest.raises(LoginError, match="timed out"):
        asyncio.run(al.antigravity_login(lambda _: None, read_code, agy, timeout=0.3))
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_cancelled_login_kills_process(monkeypatch, tmp_path):
    monkeypatch.setattr(al, "_open_browser", lambda _: None)
    pid_file = tmp_path / "pid"
    agy = fake_agy(tmp_path, f'echo $$ > "{pid_file}"\nexec sleep 30\n')

    async def read_code():
        await asyncio.sleep(30)
        return ""

    async def run():
        task = asyncio.create_task(al.antigravity_login(lambda _: None, read_code, agy))
        while not (pid_file.exists() and pid_file.read_text().strip()):
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_secret_prompt_accepts_masked_input_and_cancels():
    async def submit_code():
        with create_pipe_input() as pipe:
            dialog = SecretPrompt(
                "Antigravity sign-in",
                "Code: ",
                detail="https://accounts.google.com/o/oauth2/auth",
                input=pipe,
                output=DummyOutput(),
            )
            assert any(
                isinstance(getattr(p, "processor", p), PasswordProcessor)
                for p in (dialog.field.control.input_processors or ())
            )
            pipe.send_text("secret-code-42\r")
            return await dialog.run()

    async def cancel_escape():
        with create_pipe_input() as pipe:
            dialog = SecretPrompt("Antigravity sign-in", "Code: ", input=pipe, output=DummyOutput())
            pipe.send_text("partial\x1b")
            return await dialog.run()

    assert asyncio.run(submit_code()) == "secret-code-42"
    assert asyncio.run(cancel_escape()) is None


def test_controller_login_antigravity_uses_read_secret_and_resets_model(monkeypatch, tmp_path):
    from pcode.antigravity import AntigravityModel
    from pcode.app import PreviewApp

    monkeypatch.setattr(al, "_open_browser", lambda _: None)
    agy = fake_agy(tmp_path)
    monkeypatch.setattr(al.shutil, "which", lambda _: agy)

    model = AntigravityModel("gemini-4-neon")
    model.reset = Mock()
    runtime = SimpleNamespace(agent=SimpleNamespace(model=model), history=[])
    buffer = StringIO()
    app = PreviewApp(
        model="antigravity:gemini-4-neon",
        runtime=runtime,
        console=Console(file=buffer),
    )
    app.read_secret = AsyncMock(return_value="pasted-code-123")

    # Bare /login on an antigravity session selects antigravity.
    app.handle("/login")
    assert app.controller.login_requested == "antigravity"

    asyncio.run(app.controller.perform_login())

    app.read_secret.assert_awaited_once()
    title, prompt, detail = app.read_secret.call_args.args
    assert title == "Antigravity sign-in"
    assert prompt == "Code: "
    assert "https://accounts.google.com/o/oauth2/auth?client_id=test&state=abc" in detail
    assert (tmp_path / "code").read_text().strip() == "pasted-code-123"
    assert len(app.controller.prompts) == 0
    model.reset.assert_called_once_with()
    assert "Signed in to Antigravity." in buffer.getvalue()
    assert "pasted-code-123" not in buffer.getvalue()


def test_controller_login_antigravity_dismissed_cancels_cleanly(monkeypatch, tmp_path):
    from pcode.app import PreviewApp

    monkeypatch.setattr(al, "_open_browser", lambda _: None)
    agy = fake_agy(tmp_path)
    monkeypatch.setattr(al.shutil, "which", lambda _: agy)

    buffer = StringIO()
    app = PreviewApp(model="antigravity:gemini-4-neon", console=Console(file=buffer))
    app.read_secret = AsyncMock(return_value=None)

    app.handle("/login antigravity")
    asyncio.run(app.controller.perform_login())

    assert "Antigravity sign-in cancelled." in buffer.getvalue()
    assert app.controller.login_task is None


def test_controller_login_antigravity_refuses_when_busy_without_starting_task():
    from pcode.app import PreviewApp

    buffer = StringIO()
    app = PreviewApp(model="antigravity:gemini-4-neon", console=Console(file=buffer))
    app.controller.activity.queued_prompts.append("queued prompt")
    app.controller.login_requested = "antigravity"

    asyncio.run(app.controller.perform_login())

    assert app.controller.login_task is None
    assert "Wait for active work and queued messages to finish" in buffer.getvalue()
