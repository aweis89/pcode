"""A workspace can be deleted while a session is still working in it.

Session worktrees are removed from outside the session that owns them: another
session merges and removes it, `/worktree clean` sweeps it, someone runs `git
worktree prune`. Every tool in this process then resolves paths against a
directory that is gone.

Harness reports that as a retryable failure, on the assumption that the model
destroyed its own working directory and can `cd` out of it. Here the working
directory is the workspace, fixed for the whole run, so no command the model
can write will succeed -- a `cd /tmp` prefix does not help, because the tool
chdirs into the workspace before running it. Retrying only spends the tool
budget and ends the turn with a message blaming the model. Stop on the first
call instead, and name the directory that disappeared.
"""

from pathlib import Path

from pydantic_ai.capabilities import AbstractCapability


class WorkspaceGoneError(Exception):
    """The workspace directory disappeared; nothing in the run can recover.

    Deliberately not `ModelRetry`: the model has no move that fixes it.
    """


def require_workspace(workspace: Path) -> None:
    """Raise `WorkspaceGoneError` unless tools can still run in `workspace`."""
    if workspace.is_dir():
        return
    raise WorkspaceGoneError(
        f"The workspace {workspace} no longer exists, so no tool can run in it. "
        "It was deleted while this session was open, usually by a worktree removed "
        "from elsewhere. Resume this conversation in another directory with "
        "`pcode -C DIR --continue <session>`."
    )


class WorkspaceGuard(AbstractCapability):
    """Check the workspace still exists before each tool call."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)

    async def wrap_tool_execute(self, ctx, *, call, tool_def, args, handler):
        require_workspace(self.workspace)
        return await handler(args)
