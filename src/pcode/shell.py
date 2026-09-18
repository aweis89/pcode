"""Live shell previews, adapted from Harness 0.31's pipe-draining executor.

Keep execution, cwd capture, timeout, result formatting and output caps aligned
with the installed ShellToolset. Only run_command needs an alternate drain;
background process tools continue to use Harness unchanged.
"""

import re
import subprocess
from contextvars import ContextVar
from dataclasses import dataclass, fields

import anyio
from pydantic_ai import CapabilityEvent
from pydantic_ai_harness.shell import Shell
from pydantic_ai_harness.shell._toolset import ShellToolset, _recoverable

from pcode.tool_display import command_text

_context = ContextVar("command_preview_context", default=None)
# A quoted secret may span chunks and lines. Redact through EOF until its
# closing quote arrives, not just after a complete quoted value is available.
_OPEN_SECRET = re.compile(
    r"(?i)((?:password|passwd|secret|token|api[_-]?key|authorization)[\"']?\s*[:=]\s*)"
    r"([\"'])(.*?)(?:(?<!\\)\2|\Z)",
    re.DOTALL,
)


@dataclass(kw_only=True)
class CommandOutputEvent(CapabilityEvent, namespace="pcode_shell", name="output"):
    call_id: str
    command: str
    output: str


def preview_text(stdout, stderr):
    """Sanitize complete lines before clipping; never reveal split credentials."""
    parts = []
    for label, chunks in (("stdout", stdout), ("stderr", stderr)):
        raw = b"".join(chunks)
        end = raw.rfind(b"\n")
        if end < 0:
            continue
        text = raw[: end + 1].decode("utf-8", errors="replace")
        text = _OPEN_SECRET.sub(r"\1\2[redacted]\2", text)
        text = re.sub(r"(?i)(\btoken\s*[:=]\s*)[^\s\"',;}]+", r"\1[redacted]", text)
        parts.append(f"[{label}]\n{command_text(text).rstrip()}")
    return "\n".join(parts)[-131072:]


def toolset_options(toolset):
    names = (
        "allowed_commands",
        "denied_commands",
        "denied_operators",
        "default_timeout",
        "max_output_chars",
        "persist_cwd",
        "allow_interactive",
        "env",
        "denied_env_patterns",
    )
    return {"cwd": toolset._initial_cwd, **{name: getattr(toolset, "_" + name) for name in names}}


class StreamingShell(Shell):
    def get_toolset(self):
        return StreamingShellToolset(**toolset_options(super().get_toolset()))

    @classmethod
    def from_shell(cls, shell):
        return cls(
            **{field.name: getattr(shell, field.name) for field in fields(Shell) if field.init}
        )


class StreamingShellToolset(ShellToolset):
    async def for_run(self, ctx):
        return type(self)(**toolset_options(self))

    async def call_tool(self, name, tool_args, ctx, tool):
        token = _context.set(ctx)
        try:
            return await super().call_tool(name, tool_args, ctx, tool)
        finally:
            _context.reset(token)

    @_recoverable
    async def run_command(self, command: str, *, timeout_seconds: float | None = None) -> str:
        """Execute a shell command and return its output.

        Args:
            command: The shell command to run.
            timeout_seconds: Maximum seconds to wait (default: 30).

        Returns:
            Labeled stdout/stderr output with exit code on non-zero exit.
        """
        self._check_command(command)
        timeout = timeout_seconds if timeout_seconds is not None else self._default_timeout

        actual_command, cwd_file = self._build_cwd_capture(command)
        try:
            proc = await anyio.open_process(
                actual_command,
                cwd=self._cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                env=self._resolve_env(),
            )
            stdout_chunks: list[bytes] = []
            stderr_chunks: list[bytes] = []

            async def preview():
                previous = ""
                version = None
                while True:
                    current = (len(stdout_chunks), len(stderr_chunks))
                    if current == version:
                        await anyio.sleep(0.1)
                        continue
                    version = current
                    text = preview_text(stdout_chunks, stderr_chunks)
                    ctx = _context.get()
                    if ctx is not None and text and text != previous:
                        await ctx.emit(
                            CommandOutputEvent(
                                call_id=ctx.tool_call_id or "",
                                command=command_text(command),
                                output=text,
                            )
                        )
                        previous = text
                    await anyio.sleep(0.1)

            try:
                assert proc.stdout is not None
                assert proc.stderr is not None

                async def _read_stdout() -> None:
                    assert proc.stdout is not None
                    async for chunk in proc.stdout:
                        stdout_chunks.append(chunk)

                async def _read_stderr() -> None:
                    assert proc.stderr is not None
                    async for chunk in proc.stderr:
                        stderr_chunks.append(chunk)

                with anyio.fail_after(timeout):
                    async with anyio.create_task_group() as updates:
                        updates.start_soon(preview)
                        async with anyio.create_task_group() as tg:
                            tg.start_soon(_read_stdout)
                            tg.start_soon(_read_stderr)
                        await proc.wait()
                        updates.cancel_scope.cancel()
            except TimeoutError:
                await self._kill_process_group(proc)
                with anyio.CancelScope(shield=True):
                    await proc.wait()
                    await self._drain_with_timeout(stdout_chunks, stderr_chunks, proc)
                return f"[Command timed out after {timeout}s]"
            finally:
                # Cancellation must terminate descendants, not just close pipes.
                with anyio.CancelScope(shield=True):
                    if proc.returncode is None:
                        await self._kill_process_group(proc)
                        await proc.wait()
                    await proc.aclose()

            stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace")
            stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")

            parts: list[str] = []
            if stdout:
                parts.append(f"[stdout]\n{stdout}")
            if stderr:
                parts.append(f"[stderr]\n{stderr}")
            output = "\n".join(parts) if parts else "(no output)"

            exit_code = proc.returncode if proc.returncode is not None else 0

            if cwd_file is not None and exit_code == 0:
                self._apply_captured_cwd(cwd_file)

            if exit_code != 0:
                output = f"{output}\n[exit code: {exit_code}]"
            return output
        finally:
            if cwd_file is not None:
                cwd_file.unlink(missing_ok=True)
