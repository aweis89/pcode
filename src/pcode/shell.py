"""Sanitized UI projections of Harness's persistent shell events.

Execution, polling, cancellation and process ownership stay in Harness. This
module only accumulates its bounded output events for the transient preview.
"""

import re

from pydantic_ai_harness.shell import (
    CommandFinishedEvent,
    CommandOutputEvent,
    CommandStartedEvent,
)

from pcode.runtime import CommandOutput
from pcode.tool_display import command_text

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


def result_projection(content, finished=None):
    """Omit already-truncated tails that have lost their redaction context.

    Harness returns the last 16 KB, which can start inside a quoted credential
    or private-key block. Do not read the raw log to reconstruct it. Keep only
    the supervisor's trailing handles/status in the UI and inspection journal.
    Model tool results and upstream's log files are unaffected.
    """
    if not isinstance(content, str):
        return content
    output, separator, handles = content.rpartition("\nPID: ")
    if separator and (
        (finished is not None and finished.truncated)
        or len((output + "\n").encode("utf-8")) >= 16000
    ):
        return (
            "[Output tail omitted: upstream truncation removed redaction context. "
            "Raw output remains in the command log.]\nPID: " + handles
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
