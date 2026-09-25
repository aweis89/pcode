"""MCP is inert unless explicitly enabled, and its connections are turn-scoped."""

import asyncio
import json
import os
import sys
from io import StringIO
from pathlib import Path

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from pydantic_ai import Agent
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.profiles import ModelProfile
from rich.console import Console

from pcode.app import PreviewApp
from pcode.commands import SlashCompleter
from pcode.live import AgentRuntime
from pcode.mcp import MCPState, build_toolset, config_path, configured_servers, mcp_transport


def write_config(servers):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}))
    return path


def test_config_path_and_missing_file(monkeypatch, tmp_path):
    assert config_path() == tmp_path / "config" / "pcode" / "mcp.json"
    assert configured_servers() == {}
    monkeypatch.setenv("PCODE_MCP_CONFIG", str(tmp_path / "custom.json"))
    with pytest.raises(ValueError, match="not found"):
        configured_servers()
    write_config({"remote": {"url": "https://example.com/mcp"}})
    assert set(configured_servers()) == {"remote"}


@pytest.mark.parametrize(
    "data",
    ["not json", "[]", "{}", '{"mcpServers": []}', '{"mcpServers": {"bad name": {}}}'],
)
def test_invalid_config_is_local_and_safe(data):
    path = write_config({})
    path.write_text(data)
    with pytest.raises(ValueError):
        configured_servers()


def test_only_selected_config_is_expanded_and_validated(monkeypatch):
    monkeypatch.delenv("PCODE_TEST_MISSING_SECRET", raising=False)
    write_config(
        {
            "good": {"command": sys.executable, "args": ["--version"]},
            "missing": {
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer ${PCODE_TEST_MISSING_SECRET}"},
            },
            "invalid": {"command": ["secret-value"]},
        }
    )
    state = MCPState()
    assert state.toolsets() == []
    assert set(configured_servers()) == {"good", "missing", "invalid"}
    asyncio.run(state.enable("good"))
    original = state.toolsets()
    asyncio.run(state.enable("good"))
    assert state.toolsets() == original
    with pytest.raises(ValueError, match="Missing MCP environment variable"):
        asyncio.run(state.enable("missing"))
    with pytest.raises(ValueError) as error:
        asyncio.run(state.enable("invalid"))
    assert "secret-value" not in str(error.value)
    assert set(state.enabled) == {"good"}
    config_path().write_text("broken")
    state.disable("good")
    assert state.toolsets() == []


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"command": "echo", "url": "https://example.com"},
        {"url": "file:///tmp/secret-value"},
        {"command": "echo", "args": "secret-value"},
        {"url": "https://example.com", "cwd": "secret-value"},
        {"command": "echo", "disabled": True},
        {"command": " "},
    ],
)
def test_invalid_transport_does_not_expose_values(raw):
    with pytest.raises(ValueError) as error:
        build_toolset("test", raw)
    assert "secret-value" not in str(error.value)


def test_http_headers_and_stdio_environment(monkeypatch):
    monkeypatch.setenv("PCODE_TEST_MCP_TOKEN", "test-secret")
    monkeypatch.delenv("PCODE_UNSET_FOR_TEST", raising=False)
    remote = build_toolset(
        "remote",
        {
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer ${PCODE_TEST_MCP_TOKEN}"},
        },
    )
    assert remote.prefix == "mcp_remote"
    assert mcp_transport(remote).headers["Authorization"] == "Bearer test-secret"
    local = build_toolset(
        "local",
        {
            "command": sys.executable,
            "env": {
                "TOKEN": "${PCODE_TEST_MCP_TOKEN}",
                "DEFAULT": "${PCODE_UNSET_FOR_TEST:-fallback}",
            },
        },
    )
    transport = mcp_transport(local)
    assert transport.env["TOKEN"] == "test-secret"
    assert transport.env["DEFAULT"] == "fallback"
    assert transport.keep_alive is False


def make_app(tmp_path):
    async def model(messages, info):
        yield "ok"

    runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
    output = StringIO()
    app = PreviewApp(
        model="test",
        runtime=runtime,
        workspace=tmp_path,
        console=Console(file=output, width=160, color_system=None),
    )
    return app, output


def handle_command(app, text):
    result = app.handle(text)
    if app.mcp_enable_requested is not None:
        name = app.mcp_enable_requested
        app.mcp_enable_requested = None
        try:
            asyncio.run(app.enable_mcp(name))
        except Exception as error:
            app.transcript.note(str(error))
    return result


def test_commands_completion_reset_and_model_switch(tmp_path):
    write_config({"docs": {"command": sys.executable}, "other": {"url": "https://example.com/mcp"}})
    app, output = make_app(tmp_path)
    handle_command(app, "/mcp")
    assert "docs: off" in output.getvalue()
    assert "other: off" in output.getvalue()
    handle_command(app, "/mcp enable docs")
    assert set(app.runtime.mcp.enabled) == {"docs"}
    completer = SlashCompleter(app.registry)

    def completions(text):
        return [c.text for c in completer.get_completions(Document(text), CompleteEvent())]

    assert completions("/mcp en") == ["enable docs", "enable other"]
    assert completions("/mcp disable ") == ["disable docs"]
    assert completions("/mcp enable d") == ["enable docs"]
    assert handle_command(app, "/mcp enable missing") is False
    assert "Unknown MCP server" in output.getvalue()
    handle_command(app, "/mcp once docs")
    assert "Usage: /mcp" in output.getvalue()
    handle_command(app, "/mcp disable docs")
    assert app.runtime.mcp.toolsets() == []
    handle_command(app, "/mcp enable docs")
    app.runtime.replace_agent(Agent("test"))
    assert set(app.runtime.mcp.enabled) == {"docs"}
    handle_command(app, "/new")
    assert app.runtime.mcp.toolsets() == []
    assert AgentRuntime(Agent("test")).mcp.toolsets() == []


def test_busy_rejects_changes_but_allows_listing(tmp_path):
    write_config({"docs": {"command": sys.executable}})
    app, output = make_app(tmp_path)
    app.activity.busy = True
    handle_command(app, "/mcp enable docs")
    assert app.runtime.mcp.toolsets() == []
    assert "cannot be changed while working" in output.getvalue()
    handle_command(app, "/mcp list")
    assert "docs: off" in output.getvalue()


def test_list_and_disable_survive_broken_config(tmp_path):
    path = write_config({"docs": {"command": sys.executable}})
    app, output = make_app(tmp_path)
    handle_command(app, "/mcp enable docs")
    path.write_text("broken")
    handle_command(app, "/mcp list")
    assert "Cannot read MCP configuration" in output.getvalue()
    assert "docs: enabled" in output.getvalue()
    assert app.mcp_arguments() == ("list", "disable docs")
    handle_command(app, "/mcp disable docs")
    assert app.runtime.mcp.toolsets() == []


@pytest.fixture
def stdio_server(tmp_path):
    """Tiny real JSON-RPC server: no network, API keys, or extra server dependencies."""
    log = tmp_path / "connections.jsonl"
    script = tmp_path / "mcp_server.py"
    script.write_text("""import json, os, sys, time
with open(sys.argv[1], "a") as log:
    log.write(json.dumps({"pid": os.getpid()}) + "\\n")
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        if os.environ.get("PCODE_TEST_MCP_PHASE") == "initialize":
            time.sleep(60)
        result = {"protocolVersion": request["params"]["protocolVersion"],
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "test", "version": "1"}}
    elif method == "tools/list":
        schema = {"type": "object", "properties": {"value": {"type": "string"}},
                  "required": ["value"]}
        tools = [{"name": "echo", "description": "Echo a value", "inputSchema": schema}]
        tools += [{"name": "spare%d" % i, "description": "Spare tool %d" % i,
                   "inputSchema": schema}
                  for i in range(int(os.environ.get("PCODE_TEST_MCP_SPARE_TOOLS", "0")))]
        result = {"tools": tools}
    elif method == "tools/call":
        if os.environ.get("PCODE_TEST_MCP_PHASE") == "tool":
            with open(sys.argv[1] + ".called", "w") as marker:
                marker.write("started")
            time.sleep(60)
        result = {"content": [{"type": "text", "text": request["params"]["arguments"]["value"]}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
""")
    write_config(
        {
            # Deferred loading has its own tests below; the lifecycle tests want the
            # server's tools visible without a discovery round trip.
            "local": {
                "command": sys.executable,
                "args": [str(script), str(log)],
                "direct": True,
            },
            "unused": {"command": "/does/not/exist"},
        }
    )
    return log


def local_search_model(stream_function):
    """Exercise the local `search_tools` fallback.

    FunctionModel claims every native tool by default, including the server-side tool
    search it cannot actually run; an empty set keeps discovery on our side.
    """
    return FunctionModel(
        stream_function=stream_function,
        profile=ModelProfile(supported_native_tools=frozenset()),
    )


def assert_processes_closed(log):
    for line in log.read_text().splitlines():
        pid = json.loads(line)["pid"]
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_real_stdio_tools_only_on_enabled_turns(stdio_server):
    requests = []

    async def model(messages, info):
        names = {tool.name for tool in info.function_tools}
        requests.append(names)
        if "mcp_local_echo" in names and not isinstance(messages[-1].parts[0], ToolReturnPart):
            yield {
                0: DeltaToolCall(
                    name="mcp_local_echo", json_args='{"value":"mcp-result"}', tool_call_id="echo-1"
                )
            }
        else:
            yield "done"

    runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))

    async def run():
        async def turn():
            return [event async for event in runtime.stream("hello")]

        await turn()
        assert not stdio_server.exists()
        assert requests == [set()]
        await runtime.mcp.enable("local")
        assert not stdio_server.exists()
        await turn()
        assert requests[-2:] == [{"mcp_local_echo"}, {"mcp_local_echo"}]
        assert "mcp-result" in str(runtime.history)
        assert_processes_closed(stdio_server)
        await turn()
        assert len(stdio_server.read_text().splitlines()) == 2
        assert_processes_closed(stdio_server)
        runtime.mcp.disable("local")
        await turn()
        assert requests[-1] == set()
        assert len(stdio_server.read_text().splitlines()) == 2

    asyncio.run(run())


def test_tools_are_deferred_until_searched(stdio_server):
    """Default servers cost one search call instead of every schema, and stay callable."""
    servers = configured_servers()
    del servers["local"]["direct"]
    servers["local"]["env"] = {"PCODE_TEST_MCP_SPARE_TOOLS": "8"}
    write_config(servers)
    requests = []

    async def model(messages, info):
        names = {tool.name for tool in info.function_tools}
        requests.append(names)
        if "mcp_local_echo" not in names:
            yield {
                0: DeltaToolCall(
                    name="search_tools", json_args='{"queries":["echo"]}', tool_call_id="search-1"
                )
            }
        elif len(requests) == 2:
            # `search_tools` stays offered after discovery, so call the tool only once.
            yield {
                0: DeltaToolCall(
                    name="mcp_local_echo", json_args='{"value":"mcp-result"}', tool_call_id="echo-1"
                )
            }
        else:
            yield "done"

    runtime = AgentRuntime(Agent(local_search_model(model)))

    async def run():
        await runtime.mcp.enable("local")
        async for _ in runtime.stream("hello"):
            pass
        # None of the nine tools is offered until the model searches for one.
        assert requests[0] == {"search_tools"}
        assert "mcp_local_echo" in requests[1]
        assert "mcp-result" in str(runtime.history)
        assert_processes_closed(stdio_server)

    asyncio.run(run())


def test_a_provider_that_rejects_hidden_schemas_keeps_the_session_working(stdio_server):
    """A deferral the provider refuses is a request shape, so every later turn fails too."""
    from pydantic_ai.exceptions import ModelHTTPError

    servers = configured_servers()
    del servers["local"]["direct"]
    write_config(servers)
    requests = []
    notices = []

    async def model(messages, info):
        names = {tool.name for tool in info.function_tools}
        requests.append(names)
        if len(requests) == 1:
            raise ModelHTTPError(
                400,
                "test",
                body={
                    "message": "Invalid Value: 'tools.tool_search'. tools.tool_search "
                    "requires at least one deferred tool.",
                    "param": "tools.tool_search",
                },
            )
        if "mcp_local_echo" in names and not isinstance(messages[-1].parts[0], ToolReturnPart):
            yield {
                0: DeltaToolCall(
                    name="mcp_local_echo", json_args='{"value":"mcp-result"}', tool_call_id="echo-1"
                )
            }
        else:
            yield "done"

    runtime = AgentRuntime(Agent(local_search_model(model)))
    runtime.retry_notice = notices.append
    # Nothing to gain from resending the identical request: the repair must stand alone.
    runtime.retry_attempts = 0

    async def run():
        await runtime.mcp.enable("local")
        async for _ in runtime.stream("hello"):
            pass
        # The rejected request hid the tool behind search; the retry declares it.
        assert requests[0] == {"search_tools"}
        assert "mcp_local_echo" in requests[1]
        assert "mcp-result" in str(runtime.history)
        assert notices and "local" in notices[0]
        # And the next turn keeps the server, without deferring again.
        async for _ in runtime.stream("again"):
            pass
        assert "mcp_local_echo" in requests[-1]
        assert len(notices) == 1
        assert_processes_closed(stdio_server)

    asyncio.run(run())


@pytest.mark.parametrize("direct", [False, True])
def test_worker_inherits_real_stdio_tools_and_discovery(tmp_path, stdio_server, direct):
    from pcode.agent import create_agent

    servers = configured_servers()
    servers["local"]["direct"] = direct
    write_config(servers)
    child_calls = 0

    async def model(messages, info):
        nonlocal child_calls
        names = {tool.name for tool in info.function_tools}
        results = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        if "delegate_task" in names:
            if not results:
                yield {
                    0: DeltaToolCall(
                        name="delegate_task",
                        json_args='{"agent_name":"worker","task":"Use the enabled MCP server"}',
                    )
                }
            else:
                assert "REMOTE_RESULT" in str(results[-1].content)
                yield "Done"
            return
        child_calls += 1
        if not direct and child_calls == 1:
            assert "mcp_local_echo" not in names
            yield {0: DeltaToolCall(name="search_tools", json_args='{"queries":["echo"]}')}
        elif not results or results[-1].tool_name == "search_tools":
            assert "mcp_local_echo" in names
            yield {0: DeltaToolCall(name="mcp_local_echo", json_args='{"value":"REMOTE_RESULT"}')}
        else:
            assert "REMOTE_RESULT" in str(results[-1].content)
            yield "REMOTE_RESULT"

    agent = create_agent("test", tmp_path)
    runtime = AgentRuntime(agent)

    async def run():
        await runtime.mcp.enable("local")
        with agent.override(model=local_search_model(model)):
            async for _ in runtime.stream("Delegate"):
                pass
        assert_processes_closed(stdio_server)

    asyncio.run(run())
    assert child_calls == (2 if direct else 3)


def test_direct_server_tools_skip_search(stdio_server):
    """`direct: true` trades prompt tokens for immediate availability."""

    async def model(messages, info):
        assert {tool.name for tool in info.function_tools} == {"mcp_local_echo"}
        yield "done"

    runtime = AgentRuntime(Agent(local_search_model(model)))

    async def run():
        await runtime.mcp.enable("local")
        async for _ in runtime.stream("hello"):
            pass

    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_real_stdio_cleanup_on_error_or_cancel(stdio_server, cancel):
    started = asyncio.Event()

    async def model(messages, info):
        assert {t.name for t in info.function_tools} == {"mcp_local_echo"}
        started.set()
        if cancel:
            await asyncio.Event().wait()
        raise RuntimeError("model failed")
        yield  # make this a streaming generator

    runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
    asyncio.run(runtime.mcp.enable("local"))

    async def run():
        async def turn():
            return [event async for event in runtime.stream("hello")]

        if cancel:
            task = asyncio.create_task(turn())
            await asyncio.wait_for(started.wait(), 10)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(RuntimeError, match="model failed"):
                await turn()
        assert_processes_closed(stdio_server)

    asyncio.run(run())


def test_real_stdio_cleanup_on_early_stream_close(stdio_server):
    async def model(messages, info):
        yield "first"
        await asyncio.Event().wait()

    runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
    asyncio.run(runtime.mcp.enable("local"))

    async def run():
        stream = runtime.stream("hello")
        await anext(stream)
        await stream.aclose()
        assert_processes_closed(stdio_server)

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["initialize", "tool"])
def test_cancel_during_mcp_work(stdio_server, phase):
    servers = configured_servers()
    servers["local"]["env"] = {"PCODE_TEST_MCP_PHASE": phase}
    write_config(servers)

    async def model(messages, info):
        yield {
            0: DeltaToolCall(
                name="mcp_local_echo", json_args='{"value":"wait"}', tool_call_id="wait-1"
            )
        }

    runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
    asyncio.run(runtime.mcp.enable("local"))

    async def run():
        async def turn():
            return [event async for event in runtime.stream("hello")]

        task = asyncio.create_task(turn())
        marker = stdio_server if phase == "initialize" else Path(str(stdio_server) + ".called")
        try:
            async with asyncio.timeout(10):
                while not marker.exists():
                    await asyncio.sleep(0.01)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert_processes_closed(stdio_server)

    asyncio.run(run())


def test_a_server_that_cannot_connect_costs_only_its_own_tools(stdio_server):
    """One broken server must not fail the turn and take every other tool with it."""
    requests = []

    async def model(messages, info):
        requests.append({tool.name for tool in info.function_tools})
        yield "done"

    runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
    warnings = []
    runtime.warning_notice = warnings.append
    asyncio.run(runtime.mcp.enable("local"))
    asyncio.run(runtime.mcp.enable("unused"))

    async def turn():
        return [event async for event in runtime.stream("hello")]

    asyncio.run(turn())
    assert "mcp_local_echo" in requests[-1]
    assert set(runtime.mcp.unavailable) == {"unused"}
    assert len(warnings) == 1 and "MCP server 'unused' failed to connect" in warnings[0]
    assert_processes_closed(stdio_server)

    # Retried every turn; disabling the server clears it.
    asyncio.run(turn())
    assert len(warnings) == 2
    runtime.mcp.disable("unused")
    assert runtime.mcp.unavailable == {}
    asyncio.run(turn())
    assert len(warnings) == 2 and "mcp_local_echo" in requests[-1]
    assert_processes_closed(stdio_server)
