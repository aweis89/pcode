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
# Base models offered via ACP `configOptions`, with their default and offered `thought_level`s.
MODELS: dict[str, tuple[str | None, tuple[str, ...]]] = {
    "fake-neon": ("high", ("low", "medium", "high", "xhigh")),
    "fake-flash": ("medium", ("low", "medium", "high")),
    "fake-fixed": (None, ()),
}
DEFAULT_MODEL = "fake-neon"
session_models: dict[str, str] = {}
session_thoughts: dict[str, str | None] = {}


def config_options(session_id: str) -> list[dict]:
    model_id = session_models.get(session_id, DEFAULT_MODEL)
    default_level, levels = MODELS.get(model_id, (None, ()))
    options: list[dict] = [
        {
            "id": "model",
            "name": "Model",
            "category": "model",
            "type": "select",
            "currentValue": model_id,
            "options": [{"value": name, "name": name} for name in MODELS],
        }
    ]
    if levels:
        current = session_thoughts.get(session_id, default_level)
        options.append(
            {
                "id": "thought_level",
                "name": "Thinking Level",
                "category": "thought_level",
                "type": "select",
                "currentValue": current,
                "options": [{"value": level, "name": level} for level in levels],
            }
        )
    options.append(
        {
            "id": "mode",
            "name": "Mode",
            "category": "mode",
            "type": "select",
            "currentValue": "default",
            "options": [{"value": "default", "name": "Default"}, {"value": "plan", "name": "Plan"}],
        }
    )
    return options


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
    initialized = ask(2, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}})
    child.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
    child.stdin.write("\n")
    tools = ask(3, "tools/list")["result"]["tools"]
    log({"mcp_discover": discover, "mcp_initialize": initialized["result"], "mcp_tools": tools})
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
        call_id = f"mcp-{request['id']}"
        update(
            session,
            {
                "sessionUpdate": "tool_call",
                "toolCallId": call_id,
                "name": f"mcp_pcode_{call['name']}",
                "kind": "other",
                "title": f"Run {call['name']}",
                "status": "in_progress",
                "rawInput": call["arguments"],
            },
        )
        result = call_mcp(
            servers[session][0], call["name"], call["arguments"], call.get("cancel_after")
        )
        failed = " (error)" if result.get("isError") else ""
        first = result["content"][0]
        summary = first["text"] if first.get("type") == "text" else f"<{first.get('type')}>"
        update(
            session,
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": call_id,
                "status": "failed" if failed else "completed",
                "content": [{"type": "content", "content": {"type": "text", "text": summary}}],
            },
        )
        chunk(session, "agent_message_chunk", f"tool said{failed}: {summary}")
        result = {"stopReason": "end_turn", "usage": {"inputTokens": 1, "outputTokens": 1}}
        send({"jsonrpc": "2.0", "id": request["id"], "result": result})
        running.remove(request["id"])
        return
    if "AUTH" in trigger:
        error = {"code": -32000, "message": "Authentication required"}
        send({"jsonrpc": "2.0", "id": request["id"], "error": error})
        running.remove(request["id"])
        return
    if "SLOW" in trigger or "CANCEL_TAIL" in trigger:
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
        if "CANCEL_TAIL" in trigger and stop == "cancelled":
            threading.Event().wait(0.02)
            chunk(session, "agent_message_chunk", "stale trailing cancel frame")
            update(
                session,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "stale-1",
                    "title": "stale tool",
                    "status": "completed",
                },
            )
            update(session, {"sessionUpdate": "usage_update", "used": 20, "size": 1000})
        return
    if "BADLINE" in trigger:
        with lock:
            sys.stdout.write('not json\n[1]\n{"id": [1]}\n{"method": "session/update"}\n')
            sys.stdout.flush()
    if "CRASH" in trigger:
        chunk(session, "agent_message_chunk", "about to fail")
        sys.stdout.flush()
        os._exit(3)
    if (
        "SUBAGENT_HOLD" in trigger
        or "SUBAGENT_THEN_TOOL" in trigger
        or "SUBAGENT_FAIL" in trigger
        or "SUBAGENT_CHAIN" in trigger
        or "SUBAGENT_HANG" in trigger
    ):
        failed = "SUBAGENT_FAIL" in trigger
        sub_call = {
            "toolCallId": "sub-1",
            "name": "invoke_subagent",
            "kind": "other",
            "title": "Run invoke_subagent",
            "rawInput": {"role": "Code Reviewer", "typeName": "research", "prompt": "review diff"},
        }
        update(session, {"sessionUpdate": "tool_call", "status": "in_progress", **sub_call})
        update(
            session,
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "sub-1",
                "status": "failed" if failed else "completed",
            },
        )
        if "SUBAGENT_HOLD" in trigger and "DAEMON_PING" in trigger:
            # A daemon started in an earlier turn emitting an update without rawInput
            # must not clear subagent_launched or prematurely end the hold.
            update(
                session,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "bg-1",
                    "status": "in_progress",
                },
            )
        if "SUBAGENT_THEN_TOOL" in trigger:
            edit_call = {
                "toolCallId": "after-sub-1",
                "name": "edit_file",
                "kind": "edit",
                "title": "File a.py",
                "rawInput": {"Path": "a.py"},
            }
            update(session, {"sessionUpdate": "tool_call", "status": "in_progress", **edit_call})
            update(
                session,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "after-sub-1",
                    "status": "completed",
                    "content": [{"type": "diff", "path": "a.py", "newText": "ok"}],
                },
            )
            chunk(session, "agent_message_chunk", "All done.")
        elif failed:
            chunk(session, "agent_message_chunk", "Could not launch subagent.")
        else:
            chunk(session, "agent_message_chunk", "Waiting for the code review subagent.")
        update(session, {"sessionUpdate": "usage_update", "used": 60, "size": 1_048_576})
        result = {"stopReason": "end_turn", "usage": {"inputTokens": 10, "outputTokens": 5}}
        send({"jsonrpc": "2.0", "id": request["id"], "result": result})
        running.remove(request["id"])
        if "SUBAGENT_HOLD" in trigger or "SUBAGENT_CHAIN" in trigger:

            def finish_subagent():
                # Trailing metadata/whitespace frames while waiting must not start the quiet timer.
                threading.Event().wait(0.02)
                update(
                    session,
                    {"sessionUpdate": "session_info_update", "title": "Code Review In Progress"},
                )
                chunk(session, "agent_message_chunk", "\n")
                update(session, {"sessionUpdate": "usage_update", "used": 60, "size": 1_048_576})
                # Wait longer than the test's DEFERRED_FINALIZE_DEBOUNCE before waking up.
                threading.Event().wait(0.12)
                if "SUBAGENT_CHAIN" in trigger:
                    sub2 = {
                        "toolCallId": "sub-2",
                        "name": "invoke_subagent",
                        "kind": "other",
                        "title": "Run invoke_subagent",
                        "rawInput": {"role": "Second Reviewer", "typeName": "research"},
                    }
                    update(session, {"sessionUpdate": "tool_call", "status": "in_progress", **sub2})
                    update(
                        session,
                        {
                            "sessionUpdate": "tool_call_update",
                            "toolCallId": "sub-2",
                            "status": "completed",
                        },
                    )
                    chunk(session, "agent_message_chunk", "\n\nWaiting for second reviewer.")
                    update(
                        session, {"sessionUpdate": "usage_update", "used": 75, "size": 1_048_576}
                    )
                    threading.Event().wait(0.12)
                    chunk(session, "agent_message_chunk", "\n\nSecond review clean.")
                    update(
                        session, {"sessionUpdate": "usage_update", "used": 90, "size": 1_048_576}
                    )
                    return
                chunk(session, "agent_thought_chunk", "applying review feedback")
                edit_call = {
                    "toolCallId": "review-edit-1",
                    "name": "edit_file",
                    "kind": "edit",
                    "title": "File tests/test_smooth_scroll.py",
                    "rawInput": {"Path": "tests/test_smooth_scroll.py"},
                }
                update(
                    session, {"sessionUpdate": "tool_call", "status": "in_progress", **edit_call}
                )
                update(
                    session,
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "review-edit-1",
                        "status": "completed",
                        "content": [
                            {"type": "diff", "path": "tests/test_smooth_scroll.py", "newText": "ok"}
                        ],
                    },
                )
                chunk(session, "agent_message_chunk", "\n\nReview addressed and merged.")
                update(session, {"sessionUpdate": "usage_update", "used": 95, "size": 1_048_576})

            threading.Thread(target=finish_subagent, daemon=True).start()
        return
    if (
        "BGCMD" in trigger
        or "BGDAEMON" in trigger
        or "BGSCHED" in trigger
        or "BGHANG" in trigger
        or "BGDELEGATE" in trigger
    ):
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
        if "BGDELEGATE" in trigger:
            # A non-execute tool (e.g. MCP delegation) that runs longer than
            # OPEN_REPLY_QUIET_LIMIT while the turn is already held.
            slow_call = {
                "toolCallId": "mcp-1",
                "name": "mcp_pcode_delegate_task",
                "kind": "other",
                "title": "Run delegate_task",
                "rawInput": {"task": "review"},
            }
            update(session, {"sessionUpdate": "tool_call", "status": "in_progress", **slow_call})
            threading.Event().wait(0.15)
            update(
                session,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "mcp-1",
                    "status": "completed",
                    "content": [{"type": "content", "content": {"type": "text", "text": "LGTM"}}],
                },
            )
            chunk(session, "agent_message_chunk", " Delegation finished cleanly.")
            update(session, {"sessionUpdate": "usage_update", "used": 120, "size": 1000})
            return
        chunk(session, "agent_message_chunk", " All tests passed.")
        update(session, {"sessionUpdate": "usage_update", "used": 100, "size": 1000})
        return
    if "GATE_TOOL" in trigger or "PARALLEL_TOOLS" in trigger or "LATE_INPUT" in trigger:
        gate = os.environ.get("FAKE_AGY_GATE")
        if "PARALLEL_TOOLS" in trigger:
            calls = [
                {
                    "toolCallId": "par-1",
                    "name": "read_file",
                    "kind": "read",
                    "title": "Read a.py",
                    "rawInput": {"Path": "a.py"},
                },
                {
                    "toolCallId": "par-2",
                    "name": "run_command",
                    "kind": "execute",
                    "title": "Run pytest",
                    "rawInput": {"CommandLine": "pytest"},
                },
            ]
            for call in calls:
                update(session, {"sessionUpdate": "tool_call", "status": "in_progress", **call})
        elif "LATE_INPUT" in trigger:
            update(
                session,
                {
                    "sessionUpdate": "tool_call",
                    "status": "in_progress",
                    "toolCallId": "late-1",
                    "name": "run_command",
                    "kind": "execute",
                    "title": "Run tests",
                },
            )
            calls = [
                {
                    "toolCallId": "late-1",
                    "name": "run_command",
                    "kind": "execute",
                    "rawInput": {"CommandLine": "make test"},
                }
            ]
        else:
            calls = [
                {
                    "toolCallId": "gate-1",
                    "name": "run_command",
                    "kind": "execute",
                    "title": "Run pytest",
                    "rawInput": {"CommandLine": "pytest"},
                }
            ]
            update(session, {"sessionUpdate": "tool_call", "status": "in_progress", **calls[0]})
        if gate:
            for _ in range(1000):
                if os.path.exists(gate) or cancelled.is_set():
                    break
                cancelled.wait(0.01)
        if "LATE_INPUT" in trigger:
            chunk(session, "agent_message_chunk", "before input ")
            update(
                session,
                {"sessionUpdate": "tool_call_update", "status": "in_progress", **calls[0]},
            )
            chunk(session, "agent_message_chunk", "after input ")
        for call in calls:
            update(
                session,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": call["toolCallId"],
                    "status": "completed",
                    "content": [{"type": "content", "content": {"type": "text", "text": "ok"}}],
                },
            )
        chunk(session, "agent_message_chunk", "done")
        result = {"stopReason": "end_turn", "usage": {"inputTokens": 4, "outputTokens": 2}}
        send({"jsonrpc": "2.0", "id": request["id"], "result": result})
        running.remove(request["id"])
        return
    if "COMPLETE_BG" in text:
        for cid in ("bg-1", "wake-hang-1"):
            done = {"sessionUpdate": "tool_call_update", "toolCallId": cid, "status": "completed"}
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
    if "LATER_WAKE" in trigger:

        def wake_later():
            threading.Event().wait(0.04)
            chunk(session, "agent_thought_chunk", "processing late background result")
            wake_call = {
                "toolCallId": "wake-1",
                "name": "run_command",
                "kind": "execute",
                "title": "git commit",
                "rawInput": {"CommandLine": "git commit -m done"},
            }
            update(session, {"sessionUpdate": "tool_call", "status": "in_progress", **wake_call})
            threading.Event().wait(0.02)
            update(
                session,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "wake-1",
                    "status": "completed",
                    "content": [
                        {"type": "content", "content": {"type": "text", "text": "committed"}}
                    ],
                },
            )
            chunk(session, "agent_message_chunk", "Committed after background check.")
            update(session, {"sessionUpdate": "usage_update", "used": 140, "size": 1_048_576})

        threading.Thread(target=wake_later, daemon=True).start()
    elif trigger.startswith("LATER_MCP "):
        mcp_call = json.loads(trigger[10:])

        def wake_mcp():
            threading.Event().wait(0.04)
            wake_call = {
                "toolCallId": "wake-mcp-1",
                "name": f"mcp_pcode_{mcp_call['name']}",
                "kind": "other",
                "title": f"Run {mcp_call['name']}",
                "rawInput": mcp_call["arguments"],
            }
            update(session, {"sessionUpdate": "tool_call", "status": "in_progress", **wake_call})
            # Call MCP immediately alongside tool_call (zero delay) to verify
            # ToolBridge waits for the continuation turn's begin_turn().
            mcp_result = call_mcp(servers[session][0], mcp_call["name"], mcp_call["arguments"])
            failed = " (error)" if mcp_result.get("isError") else ""
            first = mcp_result["content"][0]
            summary = first["text"] if first.get("type") == "text" else f"<{first.get('type')}>"
            update(
                session,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "wake-mcp-1",
                    "status": "failed" if failed else "completed",
                    "content": [{"type": "content", "content": {"type": "text", "text": summary}}],
                },
            )
            chunk(session, "agent_message_chunk", f"late mcp{failed}: {summary}")
            update(session, {"sessionUpdate": "usage_update", "used": 150, "size": 1_048_576})

        threading.Thread(target=wake_mcp, daemon=True).start()
    elif "LATER_HANG" in trigger:

        def wake_hang():
            threading.Event().wait(0.04)
            wake_call = {
                "toolCallId": "wake-hang-1",
                "name": "run_command",
                "kind": "execute",
                "title": "long task",
                "rawInput": {"CommandLine": "sleep 60"},
            }
            update(session, {"sessionUpdate": "tool_call", "status": "in_progress", **wake_call})
            chunk(session, "agent_message_chunk", "Continuing in background...\n\n")
            for _ in range(1000):
                if cancelled.is_set():
                    cancelled.clear()
                    return
                cancelled.wait(0.01)

        threading.Thread(target=wake_hang, daemon=True).start()
    elif "LATER" in trigger and "LATER_TITLE" not in trigger:
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
            caps: dict = {}
            if not os.environ.get("FAKE_AGY_NO_SYSTEM_PROMPT_APPEND"):
                caps["_meta"] = {"antigravity": {"systemPrompt": {"append": True}}}
            reply = {"result": {"protocolVersion": 1, "agentCapabilities": caps}}
        elif method == "session/new":
            session_id = f"s-{started_sessions()}"
            servers[session_id] = message["params"].get("mcpServers", [])
            session_models[session_id] = DEFAULT_MODEL
            session_thoughts[session_id] = MODELS[DEFAULT_MODEL][0]
            modes = {
                "currentModeId": "default",
                "availableModes": [{"id": "default"}, {"id": "plan"}],
            }
            reply = {
                "result": {
                    "sessionId": session_id,
                    "modes": modes,
                    "configOptions": config_options(session_id),
                }
            }
        elif method == "session/resume" and message["params"]["sessionId"].startswith("gone"):
            reply = {"error": {"code": -32002, "message": "Session not found"}}
        elif method == "session/resume":
            session_id = message["params"]["sessionId"]
            servers[session_id] = message["params"].get("mcpServers", [])
            session_models.setdefault(session_id, DEFAULT_MODEL)
            session_thoughts.setdefault(session_id, MODELS[DEFAULT_MODEL][0])
        elif method == "session/set_config_option":
            params = message["params"]
            session_id = params["sessionId"]
            config_id = params.get("configId")
            value = params.get("value")
            if config_id == "model":
                if value not in MODELS:
                    reply = {"error": {"code": -32602, "message": f"Unknown model ID: {value}"}}
                else:
                    changed = session_models.get(session_id) != value
                    session_models[session_id] = value
                    if changed or session_id not in session_thoughts:
                        session_thoughts[session_id] = MODELS[value][0]
                    reply = {"result": {"configOptions": config_options(session_id)}}
            elif config_id == "thought_level":
                model_id = session_models.get(session_id, DEFAULT_MODEL)
                _, levels = MODELS.get(model_id, (None, ()))
                if value not in levels:
                    reply = {
                        "error": {
                            "code": -32602,
                            "message": (
                                f'Thinking level "{value}" is not offered for model "{model_id}"'
                            ),
                        }
                    }
                else:
                    session_thoughts[session_id] = value
                    reply = {"result": {"configOptions": config_options(session_id)}}
            else:
                reply = {"error": {"code": -32602, "message": f"Unknown configId: {config_id}"}}
        send({"jsonrpc": "2.0", "id": message["id"], **reply})


if __name__ == "__main__":
    main()
