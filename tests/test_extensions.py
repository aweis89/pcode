"""User extensions load from Python files and reach the agent and the terminal."""

import asyncio
import textwrap
from io import StringIO

from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from rich.console import Console

from pcode.agent import create_agent
from pcode.app import PreviewApp
from pcode.commands import CommandRegistry
from pcode.ext import (
    EXTENSION_GUIDE,
    ExtensionAPI,
    ExtensionUI,
    discover_extensions,
    load_extensions,
    user_extension_dir,
)
from pcode.preferences import save_preferences


def write_extension(directory, name, body):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.py"
    path.write_text(textwrap.dedent(body))
    return path


GREETER = """
    def setup(pcode):
        @pcode.tool
        def greet(name: str) -> str:
            \"\"\"Greet someone.\"\"\"
            return f"Hello, {name}!"

        pcode.instructions("Greet people warmly.")
        pcode.register_command("/hello", "Say hello", lambda arg: pcode.ui.notify(f"hi {arg}"))
"""


def test_guide_ships_with_the_package():
    assert EXTENSION_GUIDE.is_file()
    assert "def setup(pcode" in EXTENSION_GUIDE.read_text()


def test_user_directory_is_searched_and_project_is_opt_in(tmp_path):
    workspace = tmp_path / "repo"
    write_extension(user_extension_dir(), "greeter", GREETER)
    write_extension(workspace / ".pcode" / "extensions", "local", GREETER)
    write_extension(user_extension_dir(), "_private", GREETER)

    found = discover_extensions(workspace)
    assert [(e.name, e.scope) for e in found] == [("greeter", "user")]

    save_preferences(project_extensions="on")
    found = discover_extensions(workspace)
    assert [(e.name, e.scope) for e in found] == [("local", "project"), ("greeter", "user")]


def test_configured_directory_and_package_extensions(tmp_path):
    extra = tmp_path / "extra"
    (extra / "pkg").mkdir(parents=True)
    (extra / "pkg" / "helpers.py").write_text("GREETING = 'yo'\n")
    (extra / "pkg" / "__init__.py").write_text(
        "from .helpers import GREETING\n\ndef setup(pcode):\n    pcode.instructions(GREETING)\n"
    )
    save_preferences(extension_dirs=str(extra))

    loaded = load_extensions(tmp_path / "repo")
    (extension,) = loaded.extensions
    assert extension.loaded, extension.error
    assert extension.scope == "configured"
    (capability,) = extension.capabilities
    assert capability.id == "ext.pkg"
    assert "yo" in str(capability.get_instructions())


def test_contributions_are_collected_and_named(tmp_path):
    write_extension(user_extension_dir(), "greeter", GREETER)
    loaded = load_extensions(tmp_path)
    (extension,) = loaded.extensions
    assert extension.loaded, extension.error
    (capability,) = extension.capabilities
    assert capability.id == "ext.greeter"
    assert "greet" in capability.get_toolset().tools
    assert [c.name for c in loaded.commands] == ["/hello"]
    assert loaded.commands[0].group == "Extensions"
    assert loaded.commands[0].free_arguments
    assert extension.summary() == "1 tool, /hello"
    # Paths inside the workspace print relative; XDG_CONFIG_HOME is under tmp_path here.
    assert loaded.report(tmp_path) == [
        "greeter (config/pcode/extensions/greeter.py): 1 tool, /hello"
    ]
    assert loaded.report(tmp_path / "elsewhere") == [f"greeter ({extension.path}): 1 tool, /hello"]


def test_failures_are_reported_not_raised(tmp_path):
    write_extension(
        user_extension_dir(), "broken", "def setup(pcode):\n    raise RuntimeError('boom')\n"
    )
    write_extension(user_extension_dir(), "no_setup", "x = 1\n")
    write_extension(user_extension_dir(), "syntax", "def setup(pcode:\n")
    write_extension(user_extension_dir(), "fine", GREETER)

    loaded = load_extensions(tmp_path)
    by_name = {e.name: e for e in loaded.extensions}
    assert by_name["fine"].loaded
    assert by_name["broken"].error == "RuntimeError: boom (broken.py:2)"
    assert "no setup(pcode) function" in by_name["no_setup"].error
    assert by_name["syntax"].error.startswith("SyntaxError")
    assert len(loaded.failed) == 3
    # A failed extension contributes nothing.
    assert len(loaded.capabilities) == 1
    assert [c.name for c in loaded.commands] == ["/hello"]


def test_bad_tool_schema_fails_at_load(tmp_path):
    write_extension(
        user_extension_dir(),
        "badtool",
        """
        def setup(pcode):
            @pcode.tool
            def nope(value: 'NotAType') -> str:
                \"\"\"Broken annotation.\"\"\"
                return value
        """,
    )
    (extension,) = load_extensions(tmp_path).extensions
    assert not extension.loaded
    assert extension.capabilities == []


def test_add_capability_names_anonymous_capabilities():
    from pydantic_ai.capabilities import Capability

    api = ExtensionAPI("mine", None, ExtensionUI())
    named = Capability(id="custom")
    anonymous = Capability()
    api.add_capability(named)
    api.add_capability(anonymous)
    assert named.id == "custom"
    assert anonymous.id == "ext.mine"
    assert api.capabilities() == [named, anonymous]


def test_hooks_reach_a_live_agent(tmp_path, monkeypatch):
    """A before_tool_execute hook can block a call and the model sees the retry."""
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    write_extension(
        user_extension_dir(),
        "guard",
        """
        from pydantic_ai import ModelRetry

        def setup(pcode):
            @pcode.hooks.on.before_tool_execute
            async def guard(ctx, *, call, tool_def, args):
                if tool_def.name == "shell" and "rm -rf" in args.get("command", ""):
                    pcode.ui.notify("blocked", "warning")
                    raise ModelRetry("Blocked by the guard extension.")
                return args

            pcode.instructions("Guard marker instruction.")
        """,
    )
    notices = []
    loaded = load_extensions(
        tmp_path, ExtensionUI(lambda text, level: notices.append((text, level)))
    )
    (extension,) = loaded.extensions
    assert extension.loaded, extension.error
    assert extension.summary() == "1 hooks"

    calls = 0

    async def respond(messages, info):
        nonlocal calls
        calls += 1
        assert "Guard marker instruction." in info.instructions
        if calls == 1:
            yield {0: DeltaToolCall(name="shell", json_args='{"command": "rm -rf /tmp/x"}')}
            return
        retry = messages[-1].parts[0]
        assert "Blocked by the guard extension." in retry.content
        yield "Understood."

    agent = create_agent("test", tmp_path, loaded.capabilities)
    result = agent.run_sync("clean up", model=FunctionModel(stream_function=respond))
    assert result.output == "Understood."
    assert notices == [("blocked", "warning")]


def test_agent_instructions_point_at_the_guide(tmp_path, monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    seen = []

    async def respond(messages, info):
        seen.append(info.instructions)
        yield "ok"

    create_agent("test", tmp_path).run_sync("hi", model=FunctionModel(stream_function=respond))
    assert str(EXTENSION_GUIDE) in seen[0]


def test_registry_unregister_removes_aliases():
    from pcode.commands import Command

    registry = CommandRegistry()
    registry.register(Command("/a", "A", lambda _: None, aliases=("/b",)))
    registry.unregister("/b")
    assert registry.find("/a") is None and registry.find("/b") is None
    assert registry.commands == []
    registry.unregister("/missing")


class FakeLoaded:
    def __init__(self, extensions):
        self.extensions = extensions


def test_extension_commands_register_after_builtins(tmp_path):
    from pcode.commands import Command
    from pcode.ext import Extension

    output = StringIO()
    app = PreviewApp(console=Console(file=output), workspace=tmp_path)
    notes = []
    app.transcript.note = lambda text: notes.append(text)
    fine = Extension("fine", tmp_path / "fine.py", "user")
    fine.commands = [
        Command("/hello", "Say hello", lambda arg: notes.append(f"hi {arg}"), free_arguments=True)
    ]
    clash = Extension("clash", tmp_path / "clash.py", "user")
    clash.commands = [Command("/help", "Steal help", lambda _: None)]
    app.extensions = FakeLoaded([fine, clash])

    app.register_extension_commands()
    assert app.extension_command_names == ["/hello"]
    assert "already exists" in output.getvalue()
    app.handle("/hello there")
    assert notes == ["hi there"]

    # A reload replaces the previous set rather than accumulating duplicates.
    app.extensions = FakeLoaded([])
    app.register_extension_commands()
    assert app.registry.find("/hello") is None
    assert app.registry.find("/help") is not None


def test_reload_rebuilds_the_agent_and_commands(tmp_path, monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    output = StringIO()
    app = PreviewApp(console=Console(file=output), workspace=tmp_path, model="test")

    async def scenario():
        await app._initialize_runtime()
        assert app.extensions.extensions == []
        assert app.registry.find("/hello") is None

        write_extension(user_extension_dir(), "greeter", GREETER)
        app.reload("")
        assert app.reload_requested
        await app.reload_extensions()
        assert not app.reload_requested
        assert app.registry.find("/hello") is not None
        ids = [getattr(c, "id", None) for c in app.runtime.agent.root_capability.capabilities]
        assert "ext.greeter" in ids
        text = output.getvalue()
        assert "Reloaded 1 extension" in text
        assert "rebuilds the prompt cache" in text

        # Removing the file and reloading drops its command and capability.
        (user_extension_dir() / "greeter.py").unlink()
        await app.reload_extensions()
        assert app.registry.find("/hello") is None
        ids = [getattr(c, "id", None) for c in app.runtime.agent.root_capability.capabilities]
        assert "ext.greeter" not in ids
        app.runtime.close()

    asyncio.run(scenario())


def test_commands_require_a_live_session(tmp_path):
    output = StringIO()
    app = PreviewApp(console=Console(file=output), workspace=tmp_path)
    app.handle("/extensions")
    app.handle("/reload")
    text = output.getvalue()
    assert "/extensions requires a live model session" in text
    assert "/reload requires a live model session" in text
