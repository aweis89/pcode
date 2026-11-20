"""A stand-in for `agy acp`: scripted ACP replies, and a log of what it received.

`tests/test_antigravity.py` installs it on PATH as `agy`. Each received
message is appended to `$FAKE_AGY_LOG` as a JSON line, after a first line
naming the environment's Google variables. A prompt's text picks a script:
`SLOW` streams until cancelled, `CRASH` exits mid-turn, `AUTH` fails before
starting, `ALWAYS` offers only "always allow" or "reject", and `BADLINE`
writes malformed messages before a normal reply.
"""

import json
import os
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


def prompt(request):
    params = request["params"]
    session = params["sessionId"]
    text = " ".join(b.get("text", "") for b in params["prompt"] if b.get("type") == "text")
    if "AUTH" in text:
        error = {"code": -32000, "message": "Authentication required"}
        send({"jsonrpc": "2.0", "id": request["id"], "error": error})
        running.remove(request["id"])
        return
    if "SLOW" in text:
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
    if "BADLINE" in text:
        with lock:
            sys.stdout.write('not json\n[1]\n{"id": [1]}\n{"method": "session/update"}\n')
            sys.stdout.flush()
    if "CRASH" in text:
        chunk(session, "agent_message_chunk", "about to fail")
        sys.stdout.flush()
        os._exit(3)
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
    if "LATER" in text:
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
            reply = {"result": {"sessionId": f"s-{started_sessions()}", "models": current}}
        elif method == "session/resume" and message["params"]["sessionId"].startswith("gone"):
            reply = {"error": {"code": -32002, "message": "Session not found"}}
        elif method == "session/set_model" and message["params"]["modelId"] == "missing":
            reply = {"error": {"code": -32602, "message": "Unknown model"}}
        send({"jsonrpc": "2.0", "id": message["id"], **reply})


if __name__ == "__main__":
    main()
