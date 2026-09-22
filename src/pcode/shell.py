"""Sanitized UI projections of the shell tool's output events.

Execution, polling and process ownership live in `pcode.jobs`; this module only
accumulates the bounded output events for the transient preview, and decides
how much of a job result is safe to show once upstream clipping has already
removed the context redaction depends on.
"""

import re

from pydantic_ai_harness.shell import (
    CommandFinishedEvent,
    CommandOutputEvent,
    CommandStartedEvent,
)

from pcode.runtime import CommandOutput
from pcode.tool_display import command_text

REDUCED_SHELL_OUTPUT = "[Shell output reduced; full output remains in the command log.]"

# A quoted secret may span chunks and lines. Redact through EOF until its
# closing quote arrives, not just after a complete quoted value is available.
_OPEN_SECRET = re.compile(
    r"(?i)((?:password|passwd|secret|token|api[_-]?key|authorization)[\"']?\s*[:=]\s*)"
    r"([\"'])(.*?)(?:(?<!\\)\2|\Z)",
    re.DOTALL,
)


def preview_text(text, *, final=False):
    """Sanitize before clipping; unfinished lines can contain split credentials."""
    if not final:
        text = text[: text.rfind("\n") + 1]
    text = _OPEN_SECRET.sub(r"\1\2[redacted]\2", text)
    text = re.sub(r"(?i)(\btoken\s*[:=]\s*)[^\s\"',;}]+", r"\1[redacted]", text)
    return command_text(text).rstrip()[-131072:]


# Every job result ends with a `[jN · outcome · elapsed]` marker and, while the
# command runs, the lines telling the model how to get back to it. That block
# is control information, never command output, so it survives both reduction
# and redaction while the body above it may not.
_JOB_MARKER = re.compile(r"^\[j\d+ · (?:running|stopped|exit -?\d+) · [^]]*\]", re.MULTILINE)


def split_envelope(content):
    """Split a job result into its command output and its control envelope.

    Returns `None` when there is no marker, which means the text is not a job
    result and must be left exactly as it is.
    """
    marker = None
    for marker in _JOB_MARKER.finditer(content):
        pass  # The last marker belongs to this call.
    if marker is None:
        return None
    return content[: marker.start()], content[marker.start() :]


def result_projection(content, finished=None):
    """Omit already-truncated tails that have lost their redaction context.

    A result carries the last 16 KB of the log, which can start inside a quoted
    credential or private-key block. Do not read the raw log to reconstruct it.
    Keep only the control envelope in the UI and inspection journal. Model tool
    results and the log files themselves are unaffected. The tool-output limiter
    marks reduced bodies explicitly, since their length no longer proves whether
    clipping removed the opening credential marker.
    """
    if not isinstance(content, str):
        return content
    split = split_envelope(content)
    if split is None:
        return content
    output, envelope = split
    if (
        content.startswith(REDUCED_SHELL_OUTPUT)
        or (finished is not None and finished.truncated)
        or len(output.encode("utf-8")) >= 16000
    ):
        return (
            "[Output tail omitted: clipping removed redaction context. "
            "Raw output remains in the command log.]\n" + envelope
        )
    return content


class ShellPreview:
    def __init__(self):
        self.commands = {}
        self.output = {}

    def update(self, event):
        call_id = event.tool_call_id or ""
        if isinstance(event, CommandStartedEvent):
            self.commands[call_id] = command_text(event.command)
            self.output[call_id] = ""
            return None
        if call_id not in self.commands:
            return None
        if isinstance(event, CommandOutputEvent):
            self.output[call_id] += event.text
            text = preview_text(self.output[call_id])
        elif isinstance(event, CommandFinishedEvent):
            text = preview_text(self.output.pop(call_id), final=True)
            if event.truncated:
                text += "\n[Live preview capped; further output is in the command log.]"
            command = self.commands.pop(call_id)
            return CommandOutput(call_id, command, text)
        else:
            return None
        return CommandOutput(call_id, self.commands[call_id], text) if text else None
