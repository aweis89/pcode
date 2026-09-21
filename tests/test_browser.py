"""The bundled browser extension: off by default, one session across runs, a sub-agent.

No Chromium here. The launch is Harness's; what pcode adds is the lifecycle
around it, and a fake page stands in for the launched browser.
"""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

import pcode.browser as browser_state
from pcode.agent import create_agent
from pcode.browser import BrowserState
from pcode.ext import ExtensionUI, load_extensions


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch, tmp_path):
    monkeypatch.setattr(browser_state, "STATE", BrowserState())
    monkeypatch.setattr("pcode.extensions.browser.STATE", browser_state.STATE, raising=False)
    # A Chrome that exists but is never started: the session is built for a
    # port nothing answers, and tests that would launch stub `ensure_chrome`.
    fake_chrome = tmp_path / "fake-chrome"
    fake_chrome.touch()
    monkeypatch.setenv("PCODE_BROWSER_CHROME", str(fake_chrome))
    monkeypatch.delenv("PCODE_BROWSER_CDP_URL", raising=False)
    yield browser_state.STATE


def browser_extension(workspace, ui=None):
    loaded = load_extensions(workspace, ui)
    (extension,) = [e for e in loaded.extensions if e.name == "browser"]
    assert extension.loaded, extension.error
    return loaded, extension


def test_off_by_default_contributes_only_the_command(tmp_path):
    loaded, extension = browser_extension(tmp_path)
    assert extension.capabilities == []
    assert extension.subagents == []
    assert [c.name for c in extension.commands] == ["/browser"]
    assert extension.summary() == "/browser"
    assert loaded.subagents == []
    assert len(extension.closers) == 1


def test_on_adds_the_tools_and_a_subagent_sharing_one_session(tmp_path, fresh_state):
    fresh_state.enabled = True
    loaded, extension = browser_extension(tmp_path)
    assert extension.summary() == "20 tools, @browser, /browser"
    assert [c.id for c in extension.capabilities] == ["browser", "ext.browser"]
    capability = extension.capabilities[0]
    (delegate,) = loaded.subagents
    assert delegate.resolved_name == "browser"
    # Parent and child drive the same toolset, so the child sees the parent's login.
    child = next(c for c in delegate.agent.root_capability.capabilities if c.id == "browser")
    assert child.get_toolset().toolsets[1] is fresh_state.toolset
    assert "browser_open" in capability.get_toolset().toolsets[0].tools
    assert fresh_state.cdp_url.startswith("http://127.0.0.1:")


def test_command_toggles_state_and_requests_a_reload(tmp_path, fresh_state, monkeypatch):
    reloads = []
    notices = []
    ui = ExtensionUI(lambda text, level: notices.append(level), lambda: reloads.append(True))
    _, extension = browser_extension(tmp_path, ui)
    (command,) = extension.commands

    async def nothing():
        return ""

    monkeypatch.setattr(fresh_state, "arm", nothing)
    monkeypatch.setattr(fresh_state, "ensure_chrome", nothing)
    command.handler("")
    assert notices == ["info"]
    with pytest.raises(ValueError, match="already off"):
        command.handler("off")
    command.handler("launch")
    assert fresh_state.enabled and reloads == [True]
    with pytest.raises(ValueError, match="already open"):
        command.handler("launch")
    command.handler("off")
    assert not fresh_state.enabled and reloads == [True, True]
    assert fresh_state.session is None


def test_launch_turns_on_and_starts_chrome(tmp_path, fresh_state, monkeypatch):
    reloads = []
    notices = []
    ui = ExtensionUI(lambda text, level: notices.append((level, text)), lambda: reloads.append(1))
    _, extension = browser_extension(tmp_path, ui)
    started = []

    async def ensure_chrome():
        started.append(fresh_state.cdp_url)
        return "Started Chrome."

    async def navigate(url):
        return url

    monkeypatch.setattr(BrowserState, "ensure_chrome", staticmethod(ensure_chrome))
    monkeypatch.setattr(fresh_state, "arm", ensure_chrome)
    extension.commands[0].handler("launch")
    assert fresh_state.enabled and reloads == [1]
    # `open()` ran, so the session exists before the reload re-imports the extension.
    assert started and fresh_state.session is not None
    assert ("info", "Started Chrome.") in notices
    # The port nothing answers on surfaces as a notice, not a crash.
    assert notices[-1][0] == "error"


def test_a_refused_reload_leaves_state_untouched(tmp_path, fresh_state):
    def refuse():
        raise ValueError("busy")

    _, extension = browser_extension(tmp_path, ExtensionUI(None, refuse))
    with pytest.raises(ValueError, match="busy"):
        extension.commands[0].handler("launch")
    assert not fresh_state.enabled and fresh_state.session is None


def test_delegate_task_lists_the_browser_agent(tmp_path, monkeypatch, fresh_state):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    fresh_state.enabled = True
    loaded, _ = browser_extension(tmp_path)
    seen = []

    async def respond(messages, info):
        seen.append(info)
        yield "ok"

    agent = create_agent("test", tmp_path, loaded.capabilities, loaded.subagents)
    agent.run_sync("hi", model=FunctionModel(stream_function=respond))
    assert "- browser:" in seen[0].instructions
    names = {tool.name for tool in seen[0].function_tools}
    assert {"delegate_task", "browser_open", "navigate", "snapshot"} <= names


def test_browser_open_fronts_the_current_page(tmp_path, fresh_state, monkeypatch):
    """A sign-in page the user must act on is shown, not replaced with about:blank."""
    fresh_state.enabled = True
    _, extension = browser_extension(tmp_path)
    open_tool = extension.capabilities[0].get_toolset().toolsets[0].tools["browser_open"]
    page = SimpleNamespace(url="https://x/login", fronted=False)

    async def bring_to_front():
        page.fronted = True

    page.bring_to_front = bring_to_front
    navigated = []

    async def navigate(url):
        navigated.append(url)
        fresh_state.session.page = page
        return "navigated"

    async def nothing():
        return ""

    monkeypatch.setattr(fresh_state.toolset, "navigate", navigate)
    monkeypatch.setattr(fresh_state, "arm", nothing)
    monkeypatch.setattr(fresh_state, "ensure_chrome", nothing)
    assert (
        asyncio.run(open_tool.function())
        == "The browser window is in front, showing https://x/login."
    )
    assert navigated == ["about:blank"] and page.fronted
    # With a page already open nothing is navigated.
    assert asyncio.run(open_tool.function()).endswith("https://x/login.")
    assert navigated == ["about:blank"]


def test_browser_tabs_lists_the_users_tabs_and_marks_ours(tmp_path, fresh_state, monkeypatch):
    """Tabs come from Chrome's target list, never from the pages, so a hung tab cannot stall it."""
    fresh_state.enabled = True
    _, extension = browser_extension(tmp_path)
    tabs = extension.capabilities[0].get_toolset().toolsets[0].tools["browser_tabs"]
    fresh_state.session.pages = [SimpleNamespace(url="about:blank")]

    async def nothing():
        return ""

    async def list_tabs():
        return [
            ("Inbox", "https://mail.example.com/inbox"),
            ("", "https://x/"),
            ("", "about:blank"),
        ]

    monkeypatch.setattr(fresh_state, "arm", nothing)
    monkeypatch.setattr(fresh_state, "ensure_chrome", nothing)
    monkeypatch.setattr(fresh_state, "list_tabs", list_tabs)
    assert asyncio.run(tabs.function()).splitlines() == [
        "- Inbox: https://mail.example.com/inbox",
        "- (untitled): https://x/",
        "- (untitled) (yours): about:blank",
    ]


def test_list_tabs_asks_chromes_target_endpoint(fresh_state, monkeypatch):
    import httpx

    fresh_state.cdp_url = "ws://127.0.0.1:9333/devtools/browser/abc"
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(
            200,
            json=[
                {"type": "page", "title": "Inbox", "url": "https://mail.example.com/"},
                {"type": "service_worker", "title": "sw", "url": "https://mail.example.com/sw.js"},
            ],
        )

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    assert asyncio.run(fresh_state.list_tabs()) == [("Inbox", "https://mail.example.com/")]
    assert seen == ["http://127.0.0.1:9333/json/list"]
    fresh_state.cdp_url = None
    assert asyncio.run(fresh_state.list_tabs()) == []


def test_guidance_says_logins_persist_per_mode(tmp_path, fresh_state, monkeypatch):
    fresh_state.enabled = True
    _, extension = browser_extension(tmp_path)
    text = str(extension.capabilities[0].get_instructions())
    assert "persists between conversations" in text

    asyncio.run(fresh_state.close())
    monkeypatch.setenv("PCODE_BROWSER_CDP_URL", "ws://127.0.0.1:1/devtools/browser/x")
    fresh_state.enabled = fresh_state.attach = True
    _, extension = browser_extension(tmp_path)
    assert "user's own Chrome" in str(extension.capabilities[0].get_instructions())


def test_close_is_safe_before_launch_and_drops_the_session(fresh_state):
    fresh_state.open()
    assert fresh_state.session is not None and not fresh_state.launched
    asyncio.run(fresh_state.close())
    assert fresh_state.session is None and fresh_state.toolset is None
    assert fresh_state.cdp_url is None


def test_without_chrome_the_session_falls_back_to_chromium(fresh_state, monkeypatch):
    monkeypatch.setenv("PCODE_BROWSER_CHROME", "")
    monkeypatch.setattr(browser_state, "CHROME_CANDIDATES", ("/nonexistent/chrome",))
    fresh_state.enabled = True
    fresh_state.open()
    assert fresh_state.cdp_url is None
    assert "no Chrome found" in fresh_state.describe()
    assert asyncio.run(fresh_state.ensure_chrome()) == ""


def test_attach_joins_a_running_chrome_and_never_launches(fresh_state, monkeypatch):
    monkeypatch.setenv("PCODE_BROWSER_CDP_URL", "http://127.0.0.1:9222")
    fresh_state.enabled = True
    fresh_state.attach = True
    fresh_state.open()
    assert fresh_state.attached and fresh_state.cdp_url == "http://127.0.0.1:9222"
    assert asyncio.run(fresh_state.ensure_chrome()) == ""
    assert fresh_state.process is None
    assert "attached to your Chrome" in fresh_state.describe()
    asyncio.run(fresh_state.close())
    assert not fresh_state.attach


def test_attach_reads_chromes_port_file(fresh_state, monkeypatch, tmp_path):
    from pcode.browser import running_chrome_url

    port_file = tmp_path / "DevToolsActivePort"
    monkeypatch.setenv("PCODE_BROWSER_PORT_FILE", str(port_file))
    assert running_chrome_url() is None
    port_file.write_text("9333\n/devtools/browser/abc\n")
    assert running_chrome_url() == "ws://127.0.0.1:9333/devtools/browser/abc"


def test_attach_command_fails_cleanly_without_any_chrome(tmp_path, fresh_state, monkeypatch):
    monkeypatch.setenv("PCODE_BROWSER_PORT_FILE", str(tmp_path / "missing"))
    monkeypatch.setenv("PCODE_BROWSER_CHROME", "/nonexistent/chrome")
    reloads = []
    _, extension = browser_extension(tmp_path, ExtensionUI(None, lambda: reloads.append(1)))
    with pytest.raises(ValueError, match="No running Chrome"):
        extension.commands[0].handler("attach")
    assert not fresh_state.attach and not fresh_state.enabled and reloads == []


def test_attach_opens_the_debugging_switch_when_chrome_is_not_listening(
    tmp_path, fresh_state, monkeypatch
):
    monkeypatch.setenv("PCODE_BROWSER_PORT_FILE", str(tmp_path / "missing"))
    launched = []
    monkeypatch.setattr(browser_state, "_detach", launched.append)
    app = tmp_path / "Google Chrome.app" / "Contents" / "MacOS"
    app.mkdir(parents=True)
    (app / "Google Chrome").touch()
    monkeypatch.setenv("PCODE_BROWSER_CHROME", str(app / "Google Chrome"))
    monkeypatch.setattr(browser_state.sys, "platform", "darwin")
    notices = []
    _, extension = browser_extension(tmp_path, ExtensionUI(lambda text, _: notices.append(text)))
    extension.commands[0].handler("attach")
    page = "chrome://inspect/#remote-debugging"
    assert launched == [["open", "-a", str(tmp_path / "Google Chrome.app"), page]]
    assert "Turn the switch on" in notices[-1]
    assert not fresh_state.attach and not fresh_state.enabled

    monkeypatch.setattr(browser_state.sys, "platform", "linux")
    linux = tmp_path / "google-chrome"
    linux.touch()
    monkeypatch.setenv("PCODE_BROWSER_CHROME", str(linux))
    extension.commands[0].handler("attach")
    assert launched[-1] == [str(linux), page]


def test_command_arguments_are_described_for_completion(tmp_path):
    _, extension = browser_extension(tmp_path)
    (command,) = extension.commands
    assert set(command.argument_descriptions) == set(command.arguments)
    assert "remote debugging" in command.argument_descriptions["attach"]


def test_attach_command_turns_on_with_a_warning(tmp_path, fresh_state, monkeypatch):
    monkeypatch.setenv("PCODE_BROWSER_CDP_URL", "ws://127.0.0.1:9333/devtools/browser/x")
    reloads, notices = [], []
    ui = ExtensionUI(lambda text, level: notices.append(level), lambda: reloads.append(1))
    _, extension = browser_extension(tmp_path, ui)
    extension.commands[0].handler("attach")
    assert fresh_state.attach and fresh_state.enabled and reloads == [1]
    assert notices[-1] == "warning"
    with pytest.raises(ValueError, match="already open"):
        extension.commands[0].handler("attach")


def test_session_shares_the_browsers_default_context(fresh_state):
    """The page joins `contexts[0]` so cookies persist with the profile, and is wired."""
    fresh_state.open()
    session = fresh_state.session
    calls = []

    class Page:
        url = "about:blank"

        def on(self, *args):
            calls.append(("on", args[0]))

        async def close(self):
            calls.append("page.close")

    class Context:
        pages = []

        async def new_page(self):
            return Page()

        async def route(self, pattern, handler):
            calls.append(("route", pattern))

        async def route_web_socket(self, pattern, handler):
            calls.append(("ws", pattern))

    class Browser:
        contexts = [Context()]

        async def new_context(self, **kwargs):  # pragma: no cover
            raise AssertionError("must reuse the default context")

        async def close(self):
            calls.append("browser.close")

    async def scenario():
        session._driver_cm = SimpleNamespace(__aexit__=_aexit)
        session._driver = object()
        session._driver_entered = True
        session._connect = _connect
        await session._launch()
        assert session._context is Browser.contexts[0]
        assert session.page is not None and session.pages == [session.page]
        await session.__aexit__(None, None, None)

    async def _connect(driver):
        return Browser()

    async def _aexit(*args):
        calls.append("driver.exit")

    asyncio.run(scenario())
    # Open egress plus reachable localhost means no route guard; the page events
    # Harness listens to are still wired, and the tab is closed before disconnecting.
    assert ("on", "dialog") in calls
    assert calls[-3:] == ["page.close", "browser.close", "driver.exit"]


def test_chrome_that_never_answers_is_reported(fresh_state, monkeypatch, tmp_path):
    """A binary that exits at once yields a clear error rather than a hang."""
    fake = tmp_path / "chrome"
    fake.write_text("#!/bin/sh\nexit 3\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PCODE_BROWSER_CHROME", str(fake))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    fresh_state.enabled = True
    fresh_state.open()
    with pytest.raises(RuntimeError, match="exited at startup"):
        asyncio.run(fresh_state.ensure_chrome())
    assert (tmp_path / "state" / "pcode" / "chrome").is_dir()


def test_extension_closers_run_on_exit():
    from pcode.ext import Extension, LoadedExtensions

    closed = []

    async def ok():
        closed.append("ok")

    async def bad():
        raise RuntimeError("nope")

    first = Extension("first", None, "user")
    first.closers = [bad, ok]
    asyncio.run(LoadedExtensions([first]).close())
    assert closed == ["ok"]


def test_subagent_reaches_delegate_task(tmp_path, monkeypatch):
    """The generic plumbing: any extension can add a delegate beside the explorer."""
    from pydantic_ai import Agent

    from pcode.ext import ExtensionAPI

    monkeypatch.delenv("EXA_API_KEY", raising=False)
    api = ExtensionAPI("mine", tmp_path, ExtensionUI())
    api.subagent(Agent(name="helper", description="Helps out", instructions="Help."))
    (delegate,) = api.subagents
    calls = 0

    async def respond(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {
                0: DeltaToolCall(
                    name="delegate_task", json_args='{"agent_name": "helper", "task": "go"}'
                )
            }
            return
        yield "done"

    agent = create_agent("test", tmp_path, subagents=[delegate])
    assert agent.run_sync("hi", model=FunctionModel(stream_function=respond)).output == "done"
    assert calls == 3  # parent, child, parent
