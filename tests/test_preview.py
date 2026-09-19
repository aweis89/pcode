from io import StringIO
from unittest.mock import patch

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.cells import cell_len
from rich.console import Console

from pcode.app import PreviewApp
from pcode.commands import Command, CommandRegistry, SlashCompleter
from pcode.ui import create_prompt


def make_app(width=80):
    stream = StringIO()
    return PreviewApp(console=Console(file=stream, width=width, color_system=None)), stream


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "/",
            [
                "/login",
                "/logout",
                "/model",
                "/help",
                "/tools",
                "/errors",
                "/edits",
                "/diffs",
                "/demo",
                "/redraw",
                "/config",
                "/show-tasks",
                "/autohide-tasks",
                "/show-thinking",
                "/show-commands",
                "/theme",
                "/colors",
                "/effort",
                "/mcp",
                "/compact",
                "/autocompact",
                "/resend",
                "/context",
                "/new",
                "/tree",
                "/resume",
                "/session",
                "/quit",
            ],
        ),
        ("/ses", ["/session"]),
        ("/sessions", []),
        ("/res", ["/resend", "/resume"]),
        ("/de", ["/demo"]),
        ("/theme ", ["dark", "light", "auto"]),
        ("/theme l", ["light"]),
        ("/colors ", ["palette", "terminal"]),
        ("/colors t", ["terminal"]),
        ("/ex", ["/quit"]),
        ("hello /", []),
        ("/demo\n/", []),
        ("/missing", []),
        ("/tool", ["/tools"]),
    ],
)
def test_completion(text, expected):
    app, _ = make_app()
    completions = list(
        SlashCompleter(app.registry).get_completions(Document(text), CompleteEvent())
    )
    assert [item.text for item in completions] == expected
    if text == "/de":
        assert completions[0].start_position == -3
        assert completions[0].display_meta_text


def test_registry_guards_duplicate_names():
    registry = CommandRegistry()
    registry.register(Command("/one", "first", lambda _: None, aliases=("/alias",)))
    with pytest.raises(ValueError, match="Duplicate"):
        registry.register(Command("/alias", "collision", lambda _: None))
    assert len(registry.commands) == 1


def test_dispatch_theme_errors_reset_and_exit():
    app, stream = make_app()
    for text in ("hello", "/demo", "/theme light", "/theme invalid", "/missing", "/context"):
        app.handle(text)
    assert app.runtime.turns == 2
    assert app.transcript.theme == "light"
    assert "Usage: /theme [dark|light|auto]" in stream.getvalue()
    assert "Unknown command" in stream.getvalue()
    app.handle("/new")
    assert app.runtime.turns == 0
    app.handle("/exit")
    assert not app.running


@pytest.mark.parametrize(
    "command",
    ["/tree", "/new", "/help", "/demo", "/theme light", "/theme invalid", "/missing", "/exit"],
)
def test_commands_are_not_echoed_as_prompts(command):
    app, _ = make_app()
    with patch.object(app.transcript, "user", wraps=app.transcript.user) as user:
        assert not app.handle(f"  {command}  ")
        user.assert_not_called()


def test_preview_prompt_is_still_echoed():
    app, stream = make_app()
    with patch.object(app.transcript, "user", wraps=app.transcript.user) as user:
        assert not app.handle("  hello  ")
        user.assert_called_once_with("hello")
    assert "hello" in stream.getvalue()


@pytest.mark.parametrize("width", [24, 40, 80, 120])
@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("color_style", ["palette", "terminal"])
def test_rendering_fits_terminal(width, theme, color_style):
    app, stream = make_app(width)
    app.transcript.theme = theme
    app.transcript.color_style = color_style
    app.transcript.welcome()
    app.help("")
    app.demo("")
    app.handle("Hello 世界 👋 " + "unbroken" * 30)
    output = stream.getvalue()
    assert "世界" in output
    assert "preview only" not in output
    assert all("preview only" in call.event.detail for call in app.activity.tools.calls)
    assert all(cell_len(line) <= width for line in output.splitlines())
    assert "\x1b" not in output


@pytest.mark.parametrize(
    "keys, expected",
    [
        ("hello\r", "hello"),
        ("first\x1b\rsecond\r", "first\nsecond"),
        ("\x1b[200~one\ntwo\x1b[201~\r", "one\ntwo"),
    ],
)
def test_real_prompt_keybindings(keys, expected):
    app, _ = make_app()
    with create_pipe_input() as pipe:
        session = create_prompt(app.registry, input=pipe, output=DummyOutput())
        assert not session.app.full_screen
        assert not session.mouse_support
        pipe.send_text(keys)
        assert session.prompt() == expected


def test_prompt_accepts_completion_before_sending():
    app, _ = make_app()
    with create_pipe_input() as pipe:
        session = create_prompt(app.registry, input=pipe, output=DummyOutput())

        def complete(buffer):
            if buffer.complete_state:
                pipe.send_text("\t\r\r")

        session.default_buffer.on_completions_changed += complete
        pipe.send_text("/de")
        assert session.prompt() == "/demo"


def test_history_search_survives_compact_layout():
    app, _ = make_app()
    with create_pipe_input() as pipe:
        session = create_prompt(app.registry, input=pipe, output=DummyOutput())
        pipe.send_text("remember this\r")
        assert session.prompt() == "remember this"
        pipe.send_text("\x12remember\r\r")
        assert session.prompt() == "remember this"


@pytest.mark.parametrize("keys, exception", [("\x03", KeyboardInterrupt), ("\x04", EOFError)])
def test_prompt_interrupt_and_eof(keys, exception):
    app, _ = make_app()
    with create_pipe_input() as pipe:
        session = create_prompt(app.registry, input=pipe, output=DummyOutput())
        pipe.send_text(keys)
        with pytest.raises(exception):
            session.prompt()


def test_tool_command_is_not_available():
    app, stream = make_app()
    assert app.registry.find("/tool") is None
    for text in ("/tool", "/tool 1"):
        app.handle(text)
    assert stream.getvalue().count("Unknown command") == 2


def test_sessions_command_removed():
    app, stream = make_app()
    assert app.registry.find("/session") is not None
    assert app.registry.find("/sessions") is None
    app.handle("/help")
    assert "/sessions" not in stream.getvalue()
    assert not app.handle("/sessions")
    assert "Unknown command" in stream.getvalue()
