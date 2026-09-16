"""Tool presentation with visible inputs, credential redaction, and no raw result bodies."""

import json
import re
from pathlib import Path

from pcode.diagnostics import redact

LABELS = {
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


def target(name: str, args: dict) -> str:
    if name in {"run_command", "start_command"}:
        command = args.get("command")
        return (
            argument(command, command=True) if isinstance(command, str) else "command unavailable"
        )
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
    # Classify without echoing exception bodies or validation inputs.
    text = str(content).lower()
    for needles, reason in (
        (("no such file", "file not found"), "File not found"),
        (("permission denied", "not permitted"), "Permission denied"),
        (("timed out", "timeout"), "Timed out"),
        (("command not found", "executable not found"), "Executable not found"),
        (("hash mismatch", "conflict", "has changed"), "File changed; refresh before editing"),
        (("validation", "invalid", "required"), "Invalid tool arguments"),
    ):
        if any(needle in text for needle in needles):
            return reason
    return "Tool could not complete; details withheld"


def result_detail(name: str, args: dict, content: object, outcome: str) -> tuple[str, bool]:
    """Extract metrics from known Harness formats, with a safe unknown-format fallback."""
    where = target(name, args)
    text = content if isinstance(content, str) else ""
    failed = outcome != "success"
    if failed:
        prefix = "Retry requested" if outcome == "retry" else "Failed"
        result = f"{prefix} · {failure_reason(content)}"
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
    elif name == "run_command":
        # Harness appends this marker only for nonzero exit codes.
        match = re.search(r"\[exit code: (-?\d+)\]\s*$", text)
        if re.fullmatch(r"\[Command timed out after [\d.]+s\]", text):
            failed = True
            result = "Timed out"
        elif match:
            code = int(match[1])
            failed = code != 0
            result = f"exit {code}"
            if code == 127:
                result += " · Executable not found"
        elif text == "(no output)" or text.startswith(("[stdout]\n", "[stderr]\n")):
            result = "exit 0"
        else:
            result = "Command finished"  # Do not invent an exit code for unknown formats.
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
        result = "Succeeded"
    if name in {"search_files", "find_files", "list_directory"} and re.search(
        r"^\[\.\.\. truncated at \d+ (?:entries|matches)\]$", text, re.MULTILINE
    ):
        result += " · truncated"
    return (f"{where} → {result}" if where else result), failed
