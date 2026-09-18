"""Bounded, sanitized evidence of completed file mutations, not workspace diffs."""

import difflib
import re

from pcode.runtime import EditCompleted
from pcode.tool_display import command_text, plain

MAX_SOURCE = 256 * 1024
MAX_LINES = 4000
MAX_PATCH = 64 * 1024
MAX_PATCH_LINES = 400
_SENSITIVE = re.compile(
    r"(?i)(secret|token|password|credential|api.?key|private.?key|\.env|kubeconfig|"
    r"(?:^|/)\.kube/|(?:^|/)\.aws/|\.tfvars|\.pem$|\.key$|(?:^|/)id_(?:rsa|ed25519)(?:$|\.))"
)
_OPEN_SECRET = re.compile(
    r"(?i)((?:password|passwd|secret|token|api[_-]?key|authorization)[\"']?\s*[:=]\s*)"
    r"([\"'])(.*?)(?:(?<!\\)\2|\Z)",
    re.DOTALL,
)


def sensitive_path(path: str) -> bool:
    return bool(_SENSITIVE.search(path))


def edit_text(text: str) -> str:
    # Redact whole strings before adding diff prefixes or clipping. A credential
    # can span lines, and a streamed quoted value may not have its closing quote.
    text = _OPEN_SECRET.sub(r"\1\2[redacted]\2", text)
    text = re.sub(
        r"(?i)((?:password|passwd|secret|token|api[_-]?key|authorization)[\"']?\s*[:=]\s*)"
        r"[^\s\"',;}]+",
        r"\1[redacted]",
        text,
    )
    text = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*", "[private key redacted]", text, flags=re.S)
    return command_text(text)


def completed_change(path, before, after, *, existed=True, call_id="", omitted=""):
    """Build display evidence while raw snapshots are still local to execution."""
    operation = "edited" if existed else "created"
    if sensitive_path(path):
        return EditCompleted(call_id, "[sensitive path]", operation, omitted="Sensitive file")
    path = plain(edit_text(path), limit=None)
    if omitted or before is None:
        return EditCompleted(
            call_id, path, operation, omitted=omitted or "Before snapshot unavailable"
        )
    if max(len(before), len(after)) > MAX_SOURCE:
        return EditCompleted(call_id, path, operation, omitted="File exceeds preview size limit")
    if "\x00" in before or "\x00" in after:
        return EditCompleted(call_id, path, operation, omitted="Binary content")
    if max(len(before.splitlines()), len(after.splitlines())) > MAX_LINES:
        return EditCompleted(call_id, path, operation, omitted="File exceeds preview line limit")
    if before == after:
        return EditCompleted(call_id, path, "unchanged" if existed else "created")
    # Count actual changed source lines, even where redaction hides a change.
    raw = list(difflib.unified_diff(before.splitlines(True), after.splitlines(True), n=3))
    added = sum(line.startswith("+") for line in raw[2:])
    removed = sum(line.startswith("-") for line in raw[2:])
    lines = []
    for line in difflib.unified_diff(
        edit_text(before).splitlines(True),
        edit_text(after).splitlines(True),
        fromfile=f"a/{path}" if existed else "/dev/null",
        tofile=f"b/{path}",
        n=3,
    ):
        lines.append(line.rstrip("\n"))
        if not line.endswith("\n"):
            lines.append(r"\ No newline at end of file")
    patch = "\n".join(lines[:MAX_PATCH_LINES])
    truncated = len(lines) > MAX_PATCH_LINES or len(patch) > MAX_PATCH
    patch = patch[:MAX_PATCH]
    if not patch:
        omitted = "Changes hidden by redaction or newline normalization"
    return EditCompleted(call_id, path, operation, patch, added, removed, truncated, omitted)
