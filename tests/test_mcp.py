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
from rich.console import Console

from pcode.app import PreviewApp
from pcode.commands import SlashCompleter
from pcode.live import AgentRuntime
from pcode.mcp import MCPState, build_toolset, config_path, configured_servers


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
    state.enable("good")
    original = state.toolsets()
    state.enable("good")
    assert state.toolsets() == original
    with pytest.raises(ValueError, match="Missing MCP environment variable"):
        state.enable("missing")
    with pytest.raises(ValueError) as error:
        state.enable("invalid")
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
    assert remote.wrapped.client.transport.headers["Authorization"] == "Bearer test-secret"
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
    transport = local.wrapped.client.transport
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


def test_commands_completion_reset_and_model_switch(tmp_path):
    write_config({"docs": {"command": sys.executable}, "other": {"url": "https://example.com/mcp"}})
    app, output = make_app(tmp_path)
    app.handle("/mcp")
    assert "docs: off" in output.getvalue()
    assert "other: off" in output.getvalue()
    app.handle("/mcp enable docs")
    assert set(app.runtime.mcp.enabled) == {"docs"}
    completer = SlashCompleter(app.registry)

    def completions(text):
        return [c.text for c in completer.get_completions(Document(text), CompleteEvent())]

    assert completions("/mcp en") == ["enable docs", "enable other"]
    assert completions("/mcp disable ") == ["disable docs"]
    assert completions("/mcp enable d") == ["enable docs"]
    assert app.handle("/mcp enable missing") is False
    assert "Unknown MCP server" in output.getvalue()
    app.handle("/mcp once docs")
    assert "Usage: /mcp" in output.getvalue()
    app.handle("/mcp disable docs")
    assert app.runtime.mcp.toolsets() == []
    app.handle("/mcp enable docs")
    app.runtime.replace_agent(Agent("test"))
    assert set(app.runtime.mcp.enabled) == {"docs"}
    app.handle("/new")
    assert app.runtime.mcp.toolsets() == []
    assert AgentRuntime(Agent("test")).mcp.toolsets() == []


def test_busy_rejects_changes_but_allows_listing(tmp_path):
    write_config({"docs": {"command": sys.executable}})
    app, output = make_app(tmp_path)
    app.activity.busy = True
    app.handle("/mcp enable docs")
    assert app.runtime.mcp.toolsets() == []
    assert "cannot be changed while working" in output.getvalue()
    app.handle("/mcp list")
    assert "docs: off" in output.getvalue()


def test_list_and_disable_survive_broken_config(tmp_path):
    path = write_config({"docs": {"command": sys.executable}})
    app, output = make_app(tmp_path)
    app.handle("/mcp enable docs")
    path.write_text("broken")
    app.handle("/mcp list")
    assert "Cannot read MCP configuration" in output.getvalue()
    assert "docs: enabled" in output.getvalue()
    assert app.mcp_arguments() == ("list", "disable docs")
    app.handle("/mcp disable docs")
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
        result = {"tools": [{"name": "echo", "description": "Echo a value",
                  "inputSchema": {"type": "object", "properties": {
                      "value": {"type": "string"}}, "required": ["value"]}}]}
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
            "local": {"command": sys.executable, "args": [str(script), str(log)]},
            "unused": {"command": "/does/not/exist"},
        }
    )
    return log


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
        runtime.mcp.enable("local")
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
    runtime.mcp.enable("local")

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
    runtime.mcp.enable("local")

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
    runtime.mcp.enable("local")

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


def test_partial_startup_failure_closes_connected_server(stdio_server):
    async def model(messages, info):
        pytest.fail("Model must not run when MCP initialization fails")
        yield

    runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
    runtime.mcp.enable("local")
    runtime.mcp.enable("unused")

    async def run():
        with pytest.raises(Exception):
            async for _ in runtime.stream("hello"):
                pass
        assert_processes_closed(stdio_server)

    asyncio.run(run())
