"""A stand-in for `agy acp`: scripted ACP replies, and a log of what it received.

`tests/test_antigravity.py` installs it on PATH as `agy`. Each received
message is appended to `$FAKE_AGY_LOG` as a JSON line, after a first line
naming the environment's Google variables. A prompt's text picks a script:
`SLOW` streams until cancelled, `CRASH` exits mid-turn, `AUTH` fails before
starting, `ALWAYS` offers only "always allow" or "reject", and `BADLINE`
writes malformed messages before a normal reply. `MCP {"name": ..., "arguments":
...}` starts the session's first MCP server as agy does, logs `mcp_tools` and
`mcp_result`, and replies with the call's result. `/compact` reports the
context left and says nothing.
"""

import json
import os
import subprocess
import sys
import threading

LOG = os.environ["FAKE_AGY_LOG"]
lock = threading.Lock()
waiting: dict = {}
cancelled = threading.Event()
# Prompts a newer one arrived during: real agy then ends the running prompt
# at once and carries on under the new one (steering).
superseded: set = set()
running: list = []
# Each session's `mcpServers`, from session/new or session/resume.
servers: dict = {}


def log(entry):
    with open(LOG, "a") as handle:
        handle.write(json.dumps(entry) + "\n")


def started_sessions() -> int:
    """Sessions created so far by every fake process, so IDs never repeat."""
    with open(LOG) as handle:
        return sum('"session/new"' in line for line in handle)


def send(message):
    with lock:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def update(session, body):
    params = {"sessionId": session, "update": body}
    send({"jsonrpc": "2.0", "method": "session/update", "params": params})


def chunk(session, kind, text):
    update(session, {"sessionUpdate": kind, "content": {"type": "text", "text": text}})


def ask_permission(session, call, options):
    answered = threading.Event()
    waiting["perm"] = answered
    params = {"sessionId": session, "toolCall": call, "options": options}
    send({"jsonrpc": "2.0", "id": "perm", "method": "session/request_permission", "params": params})
    answered.wait(10)


def call_mcp(server, name, arguments, cancel_after=None):
    """Start `server`, list its tools and call one, with agy's opening moves.

    With `cancel_after`, give up on the call after that many seconds instead.
    """
    env = {**os.environ, **{e["name"]: e["value"] for e in server.get("env", [])}}
    child = subprocess.Popen(
        [server["command"], *server["args"]],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=env,
        text=True,
    )

    def ask(request_id, method, params=None):
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        child.stdin.write(json.dumps(message) + "\n")
        child.stdin.flush()
        return json.loads(child.stdout.readline())

    discover = ask(1, "server/discover")  # agy 1.5.13 tries this first.
    ask(2, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}})
    child.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
    child.stdin.write("\n")
    tools = ask(3, "tools/list")["result"]["tools"]
    log({"mcp_discover": discover, "mcp_tools": tools})
    if cancel_after is not None:
        call = {"name": name, "arguments": arguments}
        child.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": call})
        )
        child.stdin.write("\n")
        child.stdin.flush()
        threading.Event().wait(cancel_after)
        cancel = {"method": "notifications/cancelled", "params": {"requestId": 4}}
        child.stdin.write(json.dumps({"jsonrpc": "2.0", **cancel}) + "\n")
        child.stdin.flush()
        threading.Event().wait(0.5)  # Long enough for a reply that must not come.
        child.stdin.close()
        leftover = child.stdout.read()
        child.wait(10)
        log({"mcp_after_cancel": leftover})
        return {"content": [{"type": "text", "text": "gave up"}]}
    result = ask(4, "tools/call", {"name": name, "arguments": arguments})
    log({"mcp_result": result})
    child.stdin.close()
    child.wait(10)
    return result["result"]


def prompt(request):
    params = request["params"]
    session = params["sessionId"]
    blocks = [b.get("text", "") for b in params["prompt"] if b.get("type") == "text"]
    text = " ".join(blocks)
    trigger = blocks[-1] if blocks else ""
    if trigger.startswith("/compact"):
        # As agy does: no reply text, and the context it reports afterwards.
        update(session, {"sessionUpdate": "usage_update", "used": 800, "size": 1000})
        result = {"stopReason": "end_turn", "usage": {"inputTokens": 1, "outputTokens": 1}}
        send({"jsonrpc": "2.0", "id": request["id"], "result": result})
        running.remove(request["id"])
        return
    if trigger.startswith("MCP "):
        call = json.loads(trigger[4:])
        result = call_mcp(
            servers[session][0], call["name"], call["arguments"], call.get("cancel_after")
        )
        failed = " (error)" if result.get("isError") else ""
        chunk(session, "agent_message_chunk", f"tool said{failed}: {result['content'][0]['text']}")
        result = {"stopReason": "end_turn", "usage": {"inputTokens": 1, "outputTokens": 1}}
        send({"jsonrpc": "2.0", "id": request["id"], "result": result})
        running.remove(request["id"])
        return
    if "AUTH" in trigger:
        error = {"code": -32000, "message": "Authentication required"}
        send({"jsonrpc": "2.0", "id": request["id"], "error": error})
        running.remove(request["id"])
        return
    if "SLOW" in trigger:
        chunk(session, "agent_message_chunk", "working")
        for _ in range(1000):
            if cancelled.is_set() or request["id"] in superseded:
                break
            cancelled.wait(0.01)
        stop = "end_turn" if request["id"] in superseded else "cancelled"
        if stop == "cancelled":
            cancelled.clear()
        usage = {"inputTokens": 1, "outputTokens": 1}
        result = {"stopReason": stop, "usage": usage}
        send({"jsonrpc": "2.0", "id": request["id"], "result": result})
        running.remove(request["id"])
        return
    if "BADLINE" in trigger:
        with lock:
            sys.stdout.write('not json\n[1]\n{"id": [1]}\n{"method": "session/update"}\n')
            sys.stdout.flush()
    if "CRASH" in trigger:
        chunk(session, "agent_message_chunk", "about to fail")
        sys.stdout.flush()
        os._exit(3)
    if "BGCMD" in trigger or "BGDAEMON" in trigger or "BGSCHED" in trigger or "BGHANG" in trigger:
        is_daemon = "BGDAEMON" in trigger
        is_sched = "BGSCHED" in trigger
        call = (
            {
                "toolCallId": "bg-1",
                "name": "schedule",
                "kind": "other",
                "title": "Running schedule",
                "rawInput": {},
            }
            if is_sched
            else {
                "toolCallId": "bg-1",
                "name": "run_command",
                "kind": "execute",
                "title": "uv run pytest",
                "rawInput": {"CommandLine": "uv run pytest", "IsDaemon": is_daemon},
            }
        )
        update(session, {"sessionUpdate": "tool_call", "status": "in_progress", **call})
        chunk(session, "agent_message_chunk", "Waiting for background work...")
        result = {"stopReason": "end_turn", "usage": {"inputTokens": 5, "outputTokens": 3}}
        send({"jsonrpc": "2.0", "id": request["id"], "result": result})
        running.remove(request["id"])
        if is_daemon or "BGHANG" in text:
            return
        threading.Event().wait(0.05)
        content = [{"type": "content", "content": {"type": "text", "text": "all passed"}}]
        update(
            session,
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "bg-1",
                "status": "completed",
                "content": content,
            },
        )
        chunk(session, "agent_message_chunk", " All tests passed.")
        update(session, {"sessionUpdate": "usage_update", "used": 100, "size": 1000})
        return
    if "COMPLETE_BG" in text:
        done = {"sessionUpdate": "tool_call_update", "toolCallId": "bg-1", "status": "completed"}
        update(session, done)
    chunk(session, "agent_thought_chunk", "pondering")
    call = {
        "toolCallId": "call-1",
        "name": "write_file",
        "kind": "edit",
        "title": "File a.txt",
        "rawInput": {"Path": "a.txt", "Content": "x"},
    }
    update(session, {"sessionUpdate": "tool_call", "status": "in_progress", **call})
    if "ALWAYS" in text:
        options = [
            {"optionId": "forever", "name": "Always allow", "kind": "allow_always"},
            {"optionId": "no", "name": "Reject", "kind": "reject_once"},
        ]
    else:
        options = [
            {"optionId": "deny", "name": "Deny", "kind": "reject_once"},
            {"optionId": "yes", "name": "Allow", "kind": "allow_once"},
        ]
    ask_permission(session, call, options)
    content = [{"type": "diff", "path": "a.txt", "newText": "x"}]
    body = {"sessionUpdate": "tool_call_update", "status": "completed", "content": content, **call}
    update(session, body)
    chunk(session, "agent_message_chunk", f"got: {text[-40:]}")
    if "TITLE" in text and "LATER_TITLE" not in text:
        title_update = {"sessionUpdate": "session_info_update", "title": 'Title: "Fix parser bug."'}
        update(session, title_update)
    update(session, {"sessionUpdate": "usage_update", "used": 42, "size": 1_048_576})
    result: dict = {"stopReason": "end_turn"}
    if "NO_REPLY_USAGE" not in text:
        result["usage"] = {
            "inputTokens": 10,
            "outputTokens": 5,
            "cachedReadTokens": 3,
            "cachedWriteTokens": 0,
        }
    send({"jsonrpc": "2.0", "id": request["id"], "result": result})
    running.remove(request["id"])
    if "LATER_TITLE" in text:
        update(session, {"sessionUpdate": "session_info_update", "title": "Late idle title"})
    if "LATER" in text and "LATER_TITLE" not in text:
        # A background sub-agent reporting back after the turn ended.
        chunk(session, "agent_message_chunk", "The sub-agent ")
        chunk(session, "agent_message_chunk", "found 6 files.")
        done = {"toolCallId": "late", "title": "wc -l", "status": "completed"}
        update(session, {"sessionUpdate": "tool_call_update", **done})


def main():
    google = sorted(k for k in os.environ if k.startswith(("GEMINI", "GOOGLE")))
    log({"env": google, "argv": sys.argv[1:]})
    for line in sys.stdin:
        message = json.loads(line)
        log(message)
        method = message.get("method")
        if method is None:
            if message.get("id") in waiting:
                waiting.pop(message["id"]).set()
            continue
        if method == "session/cancel":
            cancelled.set()
            continue
        if method == "session/prompt":
            ended = list(running)
            superseded.update(ended)
            # As agy does: the running prompt ends before the new one starts.
            while any(request_id in running for request_id in ended):
                threading.Event().wait(0.002)
            running.append(message["id"])
            threading.Thread(target=prompt, args=(message,), daemon=True).start()
            continue
        reply: dict = {"result": {}}
        if method == "initialize":
            reply = {"result": {"protocolVersion": 1}}
        elif method == "session/new":
            models = [{"modelId": "fake-high"}, {"modelId": "fake-low"}]
            current = {"availableModels": models, "currentModelId": "fake-high"}
            session_id = f"s-{started_sessions()}"
            servers[session_id] = message["params"].get("mcpServers", [])
            modes = {
                "currentModeId": "default",
                "availableModes": [{"id": "default"}, {"id": "plan"}],
            }
            reply = {"result": {"sessionId": session_id, "models": current, "modes": modes}}
        elif method == "session/resume" and message["params"]["sessionId"].startswith("gone"):
            reply = {"error": {"code": -32002, "message": "Session not found"}}
        elif method == "session/resume":
            servers[message["params"]["sessionId"]] = message["params"].get("mcpServers", [])
        elif method == "session/set_model" and message["params"]["modelId"] == "missing":
            reply = {"error": {"code": -32602, "message": "Unknown model"}}
        send({"jsonrpc": "2.0", "id": message["id"], **reply})


if __name__ == "__main__":
    main()
