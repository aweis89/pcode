"""Tool presentation with visible inputs and sanitized command failure excerpts."""

import json
import re
from pathlib import Path

from pcode.diagnostics import redact

LABELS = {
    "delegate_task": "Delegate",
    "read_file": "Read",
    "write_file": "Write",
    "edit_file": "Edit",
    "search_files": "Search",
    "find_files": "Find",
    "list_directory": "List",
    "create_directory": "Create directory",
    "file_info": "File info",
    "run_command": "Run",
    "start_command": "Start",
    "check_command": "Check",
    "stop_command": "Stop",
    "write_plan": "Plan",
    "inventory_agent_context": "Context",
}
_SENSITIVE = re.compile(
    r"(?i)(secret|token|password|credential|api.?key|private.?key|\.env|kubeconfig|\.tfvars)"
)


def plain(value: str, limit: int | None = 90) -> str:
    """Remove controls (including bidi/ANSI introducers) and bound terminal text."""
    text = "".join(c if c.isprintable() else " " for c in value)
    return text if limit is None or len(text) <= limit else text[: limit - 1] + "…"


def label(name: str) -> str:
    return LABELS.get(name, plain(name))


_CREDENTIAL_OPTION = re.compile(
    r"(?i)(--?(?:password|passwd|token|access[-_]token|refresh[-_]token|"
    r"api[-_]key|secret|client[-_]secret|authorization)(?:=|\s+))"
    r"(\"[^\"]*\"|'[^']*'|[^\s;|&]+)"
)


def argument(value: str, *, command: bool = False) -> str:
    if command:
        value = _CREDENTIAL_OPTION.sub(r"\1[redacted]", value)
    # Redact before formatting, so neither wrapping nor sanitization splits credentials.
    return plain(redact(value).replace("\n", r"\n").replace("\t", r"\t"), limit=None)


def command_text(value: str) -> str:
    """Redact first, then preserve layout without allowing terminal controls."""
    value = _ANSI.sub("", value)
    value = _PRIVATE_KEY.sub("[private key redacted]", value)
    value = _QUOTED_CREDENTIAL.sub(r"\1\2[redacted]\2", value)
    value = redact(_CREDENTIAL_OPTION.sub(r"\1[redacted]", value))
    return "\n".join(plain(line.expandtabs(4), limit=None) for line in value.split("\n"))


def command_preview(value: str) -> str:
    """Structural preview, not an inferred claim about a script's purpose."""
    text = command_text(value)
    lines = text.splitlines()
    first = next((line.strip() for line in lines if line.strip()), "(empty command)")
    # Inline interpreter payloads are implementation detail, not useful labels.
    inline = re.search(r"\b(?:python[\d.]*|node|ruby|perl)\s+(-c|-e)\s+", first)
    if inline and (len(first) > 100 or len(lines) > 1):
        first = first[: inline.end()].rstrip() + " … [inline code hidden]"
    elif len(lines) > 1:
        first += f" … [{len(lines) - 1} more lines]"
    return plain(first, limit=100)


def target(name: str, args: dict) -> str:
    if name == "delegate_task":
        agent = args.get("agent_name")
        task = args.get("task")
        return (
            (plain(argument(agent), 60) if isinstance(agent, str) else "agent unavailable")
            + " · "
            + (plain(argument(task), 160) if isinstance(task, str) else "assignment unavailable")
        )
    if name in {"run_command", "start_command"}:
        command = args.get("command")
        return command_preview(command) if isinstance(command, str) else "command unavailable"
    if name in {"get_page", "web_search"}:
        key = "url" if name == "get_page" else "query"
        value = args.get(key)
        return argument(value) if isinstance(value, str) else f"{key} unavailable"
    if name in {
        "read_file",
        "write_file",
        "edit_file",
        "search_files",
        "find_files",
        "list_directory",
        "create_directory",
        "file_info",
    }:
        value = args.get("path", ".")
        if not isinstance(value, str):
            return "path unavailable"
        sensitive = bool(_SENSITIVE.search(value))
        path = Path(value)
        if path.is_absolute():
            try:
                value = str(path.relative_to(Path.cwd()))
            except ValueError:
                pass
        where = "[sensitive path]" if sensitive else plain(value)
        if name in {"search_files", "find_files"} and isinstance(args.get("pattern"), str):
            pattern = json.dumps(argument(args["pattern"]), ensure_ascii=False)
            where = f"{pattern} in {where}"
            if name == "search_files" and isinstance(args.get("include_glob"), str):
                where += f" · glob {json.dumps(argument(args['include_glob']), ensure_ascii=False)}"
        return where
    return ""


def failure_reason(content: object) -> str:
    """Show actionable tool feedback, not a lossy classification of the error."""
    if isinstance(content, list):
        # Pydantic validation errors include raw inputs; retain their locations
        # and messages without dumping those inputs into the transcript.
        text = "\n".join(
            f"{'.'.join(map(str, item.get('loc', ())))}: {item['msg']}".lstrip(": ")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("msg"), str)
        )
    else:
        text = str(content) if content is not None else ""
    return command_text(text).strip() or "No error details returned."


_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)

_QUOTED_CREDENTIAL = re.compile(
    r"(?i)((?:password|passwd|secret|token|api[_-]?key|authorization)[\"']?\s*[:=]\s*)"
    r"([\"'])(.*?)(?<!\\)\2",
    re.DOTALL,
)


def command_error(content: object) -> str:
    """Prefer stderr, or stdout for tools such as pytest; keep a bounded diagnostic tail."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        # Validation errors contain an input field: show only their messages.
        text = "\n".join(
            item["msg"]
            for item in content
            if isinstance(item, dict) and isinstance(item.get("msg"), str)
        )
    else:
        return "No error output returned."
    text = re.sub(r"\n?\[exit code: -?\d+\]\s*$", "", text)
    stderr = re.search(r"(?:^|\n)\[stderr\]\n(.*)", text, re.DOTALL)
    if stderr and stderr[1].strip():
        text = stderr[1]
    else:
        text = re.sub(r"(?:^|\n)\[(?:stdout|stderr)\]\n?", "\n", text)
    # Remove escape sequences before redaction, then redact before clipping to avoid
    # leaking pieces of a credential across formatting or excerpt boundaries.
    text = _ANSI.sub("", text)
    text = _PRIVATE_KEY.sub("[private key redacted]", text)
    text = redact(_CREDENTIAL_OPTION.sub(r"\1[redacted]", text))
    lines = [plain(line.expandtabs(4), limit=None) for line in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or lines == ["(no output)"]:
        return "No error output returned."
    truncated = len(lines) > 200
    excerpt = "\n".join(lines[-200:])
    if len(excerpt) > 32000:
        excerpt = excerpt[-32000:]
        truncated = True
    return ("… earlier error output truncated\n" if truncated else "") + excerpt


def result_detail(name: str, args: dict, content: object, outcome: str) -> tuple[str, bool]:
    """Extract metrics from known Harness formats, with a safe unknown-format fallback."""
    where = target(name, args)
    text = content if isinstance(content, str) else ""
    failed = outcome != "success"
    # Harness 0.31 reports plan validation failures as ordinary string returns.
    # Do not hide these along with successful panel-only updates.
    if name in PLAN_TOOLS and (
        text.startswith(
            (
                "Plan not updated:",
                "No changes applied.",
                "Invalid status ",
                "Cannot ",
                "A step cannot ",
            )
        )
        or text.endswith("not found.")
    ):
        failed = True
    if failed:
        prefix = "Retry requested" if outcome == "retry" else "Failed"
        result = f"{prefix} · {failure_reason(content)}"
    elif name == "delegate_task":
        # A normal return without a lifecycle end can be a rejected delegation
        # (e.g. max_calls exhausted), not proof the child completed.
        result = "Returned"
    elif name == "read_file":
        numbers = re.findall(r"^\s*(\d+)\t", text, re.MULTILINE)
        result = (
            f"lines {numbers[0]}–{numbers[-1]} · {len(numbers)} lines"
            if numbers
            else "Read finished"
        )
        if re.search(r"^\.\.\. \(\d+ more lines\. Use offset=", text, re.MULTILINE):
            result += " · truncated"
        elif text.endswith("(empty file)\n"):
            result = "0 lines · empty file"
    elif name == "search_files":
        matches = re.findall(r"^(.+?):\d+:", text, re.MULTILINE)
        result = (
            f"{len(matches)} matches in {len(set(matches))} files"
            if matches
            else ("No matches" if text.strip() == "No matches found." else "Search finished")
        )
    elif name == "list_directory":
        entries = text.splitlines()
        files = sum(bool(re.search(r"\(\d+ bytes\)$", line)) for line in entries)
        directories = sum(line.endswith("/") for line in entries)
        result = (
            f"{files} files · {directories} directories"
            if files or directories
            else "Listing finished"
        )
        if text == "(empty directory)":
            result = "0 files · 0 directories"
    elif name == "find_files":
        if text.strip() == "No matches found.":
            result = "No matches"
        else:
            count = sum(
                bool(line) and not line.startswith("[... truncated") for line in text.splitlines()
            )
            result = f"{count} entries found"
    elif name == "write_file":
        match = re.match(r"Wrote (\d+) chars \((\d+) lines\)", text)
        result = f"{match[2]} lines written · {match[1]} chars" if match else "Write finished"
    elif name == "edit_file":
        old, new = args.get("old_text"), args.get("new_text")
        result = (
            f"+{len(new.splitlines())} −{len(old.splitlines())} replacement lines"
            if (isinstance(old, str) and isinstance(new, str))
            else "Edit finished"
        )
    elif name in {"run_command", "start_command", "check_command", "stop_command"}:
        # Foreground nonzero exits and completed background processes carry this marker.
        match = re.search(r"\[exit code: (-?\d+)\]\s*$", text)
        if text.startswith("[Error:"):
            failed = True
            result = failure_reason(text)
        elif re.fullmatch(r"\[Command timed out after [\d.]+s\]", text):
            failed = True
            result = "Timed out"
        elif match:
            code = int(match[1])
            failed = code != 0
            result = f"exit {code}" if failed else ""
            if code == 127:
                result += " · Executable not found"
        elif text == "(no output)" or text.startswith(("[stdout]\n", "[stderr]\n")):
            result = ""
        else:
            result = ""  # No redundant success text for unknown formats.
    elif name == "write_plan":
        items = args.get("items")
        if isinstance(items, list) and all(isinstance(item, dict) for item in items):
            done = sum(item.get("status") == "completed" for item in items)
            active = next(
                (i + 1 for i, item in enumerate(items) if item.get("status") == "in_progress"), None
            )
            result = f"{len(items)} steps · {done} complete"
            if active:
                result += f" · working on step {active}"
        else:
            result = "Plan updated"
    elif name == "inventory_agent_context":
        try:
            data = json.loads(text) if text else content
            roots = data.get("roots") if isinstance(data, dict) else None
            if not isinstance(roots, list) or not all(isinstance(root, dict) for root in roots):
                raise ValueError
            count = sum(bool(root.get("exists")) for root in roots)
            result = (
                f"{count} assistant configuration directories"
                if count
                else "No assistant configuration directories found"
            )
        except (ValueError, TypeError):
            result = "Assistant configuration inspected"
    else:
        result = ""
    if name in {"search_files", "find_files", "list_directory"} and re.search(
        r"^\[\.\.\. truncated at \d+ (?:entries|matches)\]$", text, re.MULTILINE
    ):
        result += " · truncated"
    return (f"{where} → {result}" if where and result else where or result), failed


# Successful planning calls update the pinned panel; failures remain in scrollback.
PLAN_TOOLS = frozenset(
    {
        "write_plan",
        "read_plan",
        "add_task",
        "update_task_status",
        "update_task_statuses",
        "remove_task",
        "add_subtask",
        "set_dependency",
        "get_available_tasks",
    }
)


def delegation_detail(args: dict, outcome: str) -> tuple[str, bool]:
    state = {
        "ok": "Completed",
        "timeout": "Timed out",
        "budget": "Usage budget exhausted",
        "failed": "Failed",
        "contained": "Child failed",
    }.get(outcome, "Did not complete")
    return f"{target('delegate_task', args)} → {state}", outcome != "ok"
