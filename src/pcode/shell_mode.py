"""Shell mode: `!command` typed at the prompt runs locally and reaches the model as a tool call.

The user runs the command; nothing here asks a model for anything. The result
is queued as a synthetic `shell` tool exchange (a tool call the model "made"
plus its result) so the next request shows the model what ran and what it
printed, in the shape it already knows how to read. Before queuing, the
result passes through the same `ToolOutputLimits` bands that reduce real tool
results, so a long test log spills to a `read_tool_result` handle instead of
landing whole in the context window.
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

# Imported lazily below: `app.py` imports this module, and the terminal must
# open without loading the agent stack (see tests/test_startup.py).
if TYPE_CHECKING:
    from pydantic_ai import ToolReturn
    from pydantic_ai.messages import ModelMessage

SHELL_PREFIX = "!"
TOOL_NAME = "shell"


def shell_command(text: str) -> str | None:
    """The command behind a `!` prompt, or None when the text is an ordinary message.

    `!` alone and `!!` are not commands: the first has nothing to run and the
    second is a history-expansion habit better refused than run.
    """
    text = text.strip()
    if not text.startswith(SHELL_PREFIX):
        return None
    command = text[len(SHELL_PREFIX) :].strip()
    if not command or command.startswith(SHELL_PREFIX):
        return None
    return command


@dataclass(frozen=True)
class ShellRun:
    command: str
    output: str
    exit_code: int | None
    elapsed_seconds: float

    @property
    def failed(self) -> bool:
        return self.exit_code != 0

    def tool_result(self) -> str:
        """Match Harness's `run_command` rendering, which the model already reads."""
        output = self.output
        if self.exit_code is None:
            return f"{output}\n[Command interrupted before it finished]"
        if self.exit_code != 0:
            return f"{output}\n[exit code: {self.exit_code}]"
        return output


async def execute(
    command: str,
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    on_output: Callable[[str], None] | None = None,
) -> ShellRun:
    """Run `command` to completion, streaming combined stdout/stderr to `on_output`.

    No timeout: the user typed this and can cancel it. Cancellation kills the
    whole process group, so a `make test` cannot leave its children running.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    process = await asyncio.create_subprocess_shell(
        command,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    chunks: list[str] = []
    assert process.stdout is not None
    try:
        while data := await process.stdout.read(4096):
            text = data.decode("utf-8", errors="replace")
            chunks.append(text)
            if on_output is not None:
                on_output(text)
        exit_code: int | None = await process.wait()
    except BaseException:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
        raise
    return ShellRun(command, "".join(chunks), exit_code, loop.time() - started)


def shell_exchange(
    command: str, content: str | ToolReturn, *, call_id: str | None = None, first: bool = False
) -> list[ModelMessage]:
    """Messages that show the model a `shell` call it did not make, with its result.

    `first` prepends the typed line as a user turn: providers reject a history
    that opens with an assistant message, and an empty history has no user
    turn for the response to follow. Otherwise the pair simply extends the
    conversation; Pydantic AI merges the trailing request with the user's
    next prompt, tool results first, exactly as after a real tool call.
    """
    from pydantic_ai import ToolReturn
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        ToolCallPart,
        ToolReturnPart,
        UserPromptPart,
    )

    call_id = call_id or f"shell_mode_{uuid4().hex[:12]}"
    if isinstance(content, ToolReturn):
        result_part = ToolReturnPart(
            tool_name=TOOL_NAME,
            content=content.return_value,
            tool_call_id=call_id,
            metadata=content.metadata,
        )
    else:
        result_part = ToolReturnPart(tool_name=TOOL_NAME, content=content, tool_call_id=call_id)
    messages: list[ModelMessage] = [
        ModelResponse(parts=[ToolCallPart(TOOL_NAME, {"command": command}, tool_call_id=call_id)]),
        ModelRequest(parts=[result_part]),
    ]
    if first:
        messages.insert(0, ModelRequest(parts=[UserPromptPart(f"{SHELL_PREFIX}{command}")]))
    return messages


async def reduce_result(limits, *, call_id: str, command: str, result: str):
    """Apply the agent's tool-output bands to a result produced outside a run.

    Spill and truncate only read the run id and retry count from their
    context, so a bare `RunContext` is enough; the run id namespaces the spill
    handle on disk. The model slot stays empty on purpose: an `anthropic:`
    string with no credential cannot be inferred, and nothing here calls it.
    """
    from pydantic_ai import RunContext
    from pydantic_ai.messages import ToolCallPart
    from pydantic_ai.tools import ToolDefinition
    from pydantic_ai.usage import RunUsage

    ctx = RunContext(deps=None, model=None, usage=RunUsage(), run_id=f"shell-mode-{call_id}")
    call = ToolCallPart(TOOL_NAME, {"command": command}, tool_call_id=call_id)
    return await limits.after_tool_execute(
        ctx,
        call=call,
        tool_def=ToolDefinition(name=TOOL_NAME),
        args={"command": command},
        result=result,
    )
