"""`ClaudeWorkspace`: the directory a `claude:` request's CLI runs in."""

from dataclasses import replace
from pathlib import Path

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models import ModelRequestContext

# Model setting carrying the workspace the CLI runs in (see `ClaudeWorkspace`).
CWD_SETTING = "pcode_claude_cwd"


class ClaudeWorkspace(AbstractCapability):
    """Run `claude:` requests' CLI in the agent's workspace.

    The CLI tells the model its working directory, and keeps transcripts per
    directory; pcode's process directory is not the workspace in worktree mode.
    A `fallback` one yields to any other: sub-agents get the parent's as a
    fallback, and an isolated worker's own checkout must win whatever the order.
    """

    def __init__(self, workspace: Path, *, fallback: bool = False) -> None:
        self.workspace = Path(workspace)
        self.fallback = fallback

    async def before_model_request(
        self, ctx: RunContext, request_context: ModelRequestContext
    ) -> ModelRequestContext:
        settings = request_context.model_settings or {}
        if request_context.model.system != "claude" or (self.fallback and CWD_SETTING in settings):
            return request_context
        settings = {**settings, CWD_SETTING: str(self.workspace)}
        return replace(request_context, model_settings=settings)
