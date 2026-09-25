"""User extensions load from Python files and reach the agent and the terminal."""

import asyncio
import os
import shutil
import sys
import textwrap
from io import StringIO

import pytest
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from rich.console import Console

from pcode.agent import create_agent
from pcode.app import PreviewApp
from pcode.commands import CommandRegistry
from pcode.ext import (
    EXTENSION_GUIDE,
    ExtensionAPI,
    ExtensionCapabilities,
    ExtensionUI,
    discover_extensions,
    load_extensions,
    user_extension_dir,
)
from pcode.preferences import save_preferences


@pytest.fixture(autouse=True)
def no_bundled_extensions(tmp_path, monkeypatch):
    """These tests count what they wrote; the shipped defaults are covered in test_search."""
    monkeypatch.setattr("pcode.ext.BUNDLED_DIR", tmp_path / "no-bundled")


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


def test_disabled_extension_is_listed_but_not_imported(tmp_path):
    write_extension(user_extension_dir(), "greeter", GREETER)
    write_extension(
        user_extension_dir(), "explodes", "raise RuntimeError('imported')\n\ndef setup(pcode): ..."
    )
    save_preferences(extensions_off="explodes")

    loaded = load_extensions(tmp_path)
    off = {e.name: e for e in loaded.extensions}["explodes"]
    # Off means never imported, so a module that fails at import is not a failure.
    assert not off.enabled and off.error is None
    assert loaded.failed == []
    assert loaded.disabled == [off]
    assert [c.name for c in loaded.commands] == ["/hello"]
    assert "explodes (config/pcode/extensions/explodes.py): off (/extensions on explodes)" in (
        loaded.report(tmp_path)
    )


def test_opt_in_extension_loads_only_once_enabled(tmp_path):
    write_extension(user_extension_dir(), "optional", "    DEFAULT_ENABLED = False\n" + GREETER)
    (extension,) = load_extensions(tmp_path).extensions
    assert extension.disabled == "off by default"
    assert extension.capabilities == [] and extension.commands == []

    save_preferences(extensions_on="optional")
    (extension,) = load_extensions(tmp_path).extensions
    assert extension.loaded, extension.error
    assert [c.name for c in extension.commands] == ["/hello"]


def test_extensions_command_toggles_and_reloads(tmp_path, monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    from pcode.ext import name_list

    write_extension(user_extension_dir(), "greeter", GREETER)
    output = StringIO()
    app = PreviewApp(console=Console(file=output), workspace=tmp_path, model="test")

    async def scenario():
        await app._initialize_runtime()
        app.handle("/extensions")
        assert "greeter" in output.getvalue()
        assert "on greeter" not in app.extension_arguments()
        assert "off greeter" in app.extension_arguments()

        app.handle("/extensions off greeter")
        assert app.reload_requested
        assert name_list("extensions_off") == {"greeter"}
        await app.reload_extensions()
        assert app.registry.find("/hello") is None
        assert "Reloaded 0 extensions, 1 off" in output.getvalue()
        assert app.extension_arguments() == ("list", "on greeter")

        app.handle("/extensions on greeter")
        assert name_list("extensions_off") == set()
        await app.reload_extensions()
        assert app.registry.find("/hello") is not None
        app.runtime.close()

    asyncio.run(scenario())


def test_extensions_command_rejects_bad_usage(tmp_path, monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    output = StringIO()
    app = PreviewApp(console=Console(file=output), workspace=tmp_path, model="test")

    async def scenario():
        await app._initialize_runtime()
        app.handle("/extensions enable greeter")
        app.handle("/extensions off greeter")
        app.runtime.close()

    asyncio.run(scenario())
    text = output.getvalue()
    assert "Usage: /extensions" in text
    assert "Unknown extension 'greeter'" in text


def test_commands_require_a_live_session(tmp_path):
    output = StringIO()
    app = PreviewApp(console=Console(file=output), workspace=tmp_path)
    app.handle("/extensions")
    app.handle("/reload")
    text = output.getvalue()
    assert "/extensions requires a live model session" in text
    assert "/reload requires a live model session" in text


WORKSPACE_EXTENSION = '''
def setup(pcode):
    @pcode.tool
    def write_bound() -> str:
        """Write into the workspace captured at setup."""
        (pcode.workspace / "written").write_text(__name__)
        return str(pcode.workspace)

    async def close():
        (pcode.workspace / "closed").write_text(__name__)
    pcode.on_close(close)
'''


def bound_tool(capabilities):
    return capabilities[0].get_toolset().tools["write_bound"].function


def test_worker_flag_is_false_for_parent_and_true_for_rebound_setup(tmp_path):
    write_extension(
        user_extension_dir(),
        "worker_probe",
        '''
def setup(pcode):
    assert type(pcode.is_worker) is bool
    @pcode.tool
    def is_worker() -> bool:
        """Report whether this extension belongs to a worker."""
        return pcode.is_worker
''',
    )
    loaded = load_extensions(tmp_path)
    parent_tool = loaded.capabilities[0].get_toolset().tools["is_worker"].function
    assert parent_tool() is False
    child = tmp_path / "child"
    child.mkdir()

    async def scenario():
        async with loaded.capabilities.for_workspace(child) as rebound:
            child_tool = rebound[0].get_toolset().tools["is_worker"].function
            assert child_tool() is True
            assert parent_tool() is False
        await loaded.close()

    asyncio.run(scenario())


def test_capabilities_rebind_concurrently_and_clean_package_modules(tmp_path):
    workspace = tmp_path / "parent"
    workspace.mkdir()
    package = user_extension_dir() / "writer"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("from .helper import setup\n")
    (package / "helper.py").write_text(WORKSPACE_EXTENSION)
    loaded = load_extensions(workspace)
    capabilities = loaded.capabilities
    assert isinstance(capabilities, list)
    assert isinstance(capabilities, ExtensionCapabilities)
    assert capabilities == loaded.extensions[0].capabilities
    parent_tool = bound_tool(capabilities)
    parent_module = sys.modules[parent_tool.__module__]
    assert parent_tool() == str(workspace)
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()

    async def scenario():
        ready = asyncio.Event()
        active = []

        async def worker(path):
            async with capabilities.for_workspace(path) as rebound:
                tool = bound_tool(rebound)
                active.append(tool.__module__)
                if len(active) == 2:
                    ready.set()
                await ready.wait()
                assert len(set(active + [parent_tool.__module__])) == 3
                assert all(name in sys.modules for name in active)
                assert tool() == str(path)
                assert not (path / "closed").exists()
                assert not (workspace / "closed").exists()
            assert (path / "closed").read_text() == tool.__module__
            assert tool.__module__ not in sys.modules
            assert tool.__module__.removesuffix(".helper") not in sys.modules

        await asyncio.gather(worker(first), worker(second))
        assert sys.modules[parent_tool.__module__] is parent_module
        assert not (workspace / "closed").exists()
        await loaded.close()
        assert (workspace / "closed").read_text() == parent_tool.__module__

    asyncio.run(scenario())
    assert (first / "written").read_text() != (second / "written").read_text()
    assert (workspace / "written").read_text() == parent_tool.__module__


def test_rebinding_uses_only_successful_parent_sources(tmp_path, monkeypatch):
    workspace, child = tmp_path / "parent", tmp_path / "child"
    workspace.mkdir()
    child.mkdir()
    write_extension(user_extension_dir(), "good", WORKSPACE_EXTENSION)
    broken = write_extension(user_extension_dir(), "broken", "raise RuntimeError('broken')")
    off = write_extension(user_extension_dir(), "off", WORKSPACE_EXTENSION)
    opt_in = write_extension(
        user_extension_dir(), "opt_in", "DEFAULT_ENABLED = False\n" + WORKSPACE_EXTENSION
    )
    save_preferences(extensions_off="off")
    loaded = load_extensions(workspace)
    assert len(loaded.failed) == 1
    assert len(loaded.disabled) == 2
    for path in (broken, off, opt_in):
        path.write_text("raise AssertionError('must not import')")
    write_extension(user_extension_dir(), "new", "raise AssertionError('must not discover')")
    save_preferences(extensions_off="good", extensions_on="off,opt_in")
    monkeypatch.setattr(
        "pcode.ext.discover_extensions", lambda _: pytest.fail("must not rediscover")
    )

    async def scenario():
        async with loaded.capabilities.for_workspace(child) as capabilities:
            assert len(capabilities) == 1
            assert bound_tool(capabilities)() == str(child)
        assert not (workspace / "closed").exists()
        await loaded.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure", ["RuntimeError('guardrail failed')", "asyncio.CancelledError()"]
)
def test_rebinding_failure_closes_partial_setup_and_previous_children(tmp_path, failure):
    workspace, child = tmp_path / "parent", tmp_path / "child"
    workspace.mkdir()
    child.mkdir()
    write_extension(user_extension_dir(), "a_good", WORKSPACE_EXTENSION)
    write_extension(
        user_extension_dir(),
        "z_fails",
        f"""
        import asyncio
        def setup(pcode):
            async def close():
                (pcode.workspace / "partial_closed").write_text(__name__)
            pcode.on_close(close)
            if pcode.workspace.name == "child":
                raise {failure}
        """,
    )
    loaded = load_extensions(workspace)
    assert not loaded.failed
    modules = {name for name in sys.modules if "_rebind_" in name}

    async def scenario():
        expected = asyncio.CancelledError if "CancelledError" in failure else ValueError
        with pytest.raises(expected) as caught:
            async with loaded.capabilities.for_workspace(child):
                pytest.fail("a failed guardrail must abort rebinding")
        if expected is ValueError:
            assert "z_fails: RuntimeError: guardrail failed" in str(caught.value)
        assert (child / "closed").exists()
        assert (child / "partial_closed").exists()
        assert not (workspace / "closed").exists()
        assert not (workspace / "partial_closed").exists()
        assert bound_tool(loaded.capabilities)() == str(workspace)
        assert {name for name in sys.modules if "_rebind_" in name} == modules
        await loaded.close()

    asyncio.run(scenario())


def test_cancelled_rebound_context_awaits_child_closers(tmp_path):
    workspace, child = tmp_path / "parent", tmp_path / "child"
    workspace.mkdir()
    child.mkdir()
    write_extension(
        user_extension_dir(),
        "writer",
        "import asyncio\n"
        + WORKSPACE_EXTENSION.replace(
            "    async def close():",
            "    async def close():\n"
            '        (pcode.workspace / "closing").touch()\n'
            "        await asyncio.sleep(0.01)",
        ),
    )
    loaded = load_extensions(workspace)

    async def scenario():
        ready = asyncio.Event()

        async def worker():
            async with loaded.capabilities.for_workspace(child):
                ready.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(worker())
        await ready.wait()
        task.cancel()
        async with asyncio.timeout(2):
            while not (child / "closing").exists():
                await asyncio.sleep(0)
        # A second cancellation during teardown must not cancel the closer itself.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (child / "closed").exists()
        assert not (workspace / "closed").exists()
        await loaded.close()

    asyncio.run(scenario())


def test_rebinding_preserves_session_and_notify_without_parent_reload(tmp_path):
    workspace, child = tmp_path / "parent", tmp_path / "child"
    workspace.mkdir()
    child.mkdir()
    session_dir = tmp_path / "custom_sessions"
    notices, reloads = [], []
    write_extension(
        user_extension_dir(),
        "context",
        """
        def setup(pcode):
            pcode.ui.notify(str(pcode.session_dir))
            pcode.ui.request_reload()
            pcode.register_command("/context", "Context", lambda arg: None)
            pcode.instructions(str(pcode.workspace))
        """,
    )
    loaded = load_extensions(
        workspace,
        ExtensionUI(
            notify=lambda text, level: notices.append((text, level)),
            request_reload=lambda: reloads.append(True),
        ),
        session_dir=session_dir,
    )
    commands = loaded.commands

    async def scenario():
        async with loaded.capabilities.for_workspace(child) as capabilities:
            assert str(child) in str(capabilities[0].get_instructions())
            assert notices == [(str(session_dir), "info")] * 2
            assert reloads == [True]
            assert loaded.commands == commands
        await loaded.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("trusted", [False, True])
def test_rebinding_remaps_only_trusted_project_sources(tmp_path, monkeypatch, trusted):
    workspace, child = tmp_path / "parent", tmp_path / "child"
    workspace.mkdir()
    child.mkdir()
    project = workspace / ".pcode" / "extensions"
    configured = workspace / "configured"
    bundled = tmp_path / "bundled"
    monkeypatch.setattr("pcode.ext.BUNDLED_DIR", bundled)
    write_extension(project, "project", 'def setup(pcode): pcode.instructions("parent project")')
    write_extension(
        configured, "configured", 'def setup(pcode): pcode.instructions("original configured")'
    )
    write_extension(bundled, "bundled", 'def setup(pcode): pcode.instructions("original bundled")')
    write_extension(
        user_extension_dir(), "user", 'def setup(pcode): pcode.instructions("original user")'
    )
    save_preferences(project_extensions="on" if trusted else "off", extension_dirs="configured")
    loaded = load_extensions(workspace)
    shutil.copytree(project, child / ".pcode" / "extensions")
    write_extension(
        child / ".pcode" / "extensions",
        "project",
        'def setup(pcode): pcode.instructions("child project")',
    )
    write_extension(child / "configured", "configured", "raise AssertionError('wrong source')")
    write_extension(
        child / ".pcode" / "extensions", "new", "raise AssertionError('untrusted discovery')"
    )
    cwd = os.getcwd()
    # Trust was decided at parent discovery, not by the child worktree or a new overlay.
    monkeypatch.setattr("pcode.project_trust.is_trusted", lambda _: False)

    async def scenario():
        async with loaded.capabilities.for_workspace(child) as capabilities:
            instructions = " ".join(str(c.get_instructions()) for c in capabilities)
            assert "original configured" in instructions
            assert "original bundled" in instructions
            assert "original user" in instructions
            assert ("child project" in instructions) is trusted
            assert "parent project" not in instructions
            assert os.getcwd() == cwd
        await loaded.close()

    asyncio.run(scenario())
