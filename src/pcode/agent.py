"""Agent construction is independent of the terminal and runtime adapter."""

import os
from dataclasses import fields
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.capabilities import CombinedCapability
from pydantic_ai.models.openai_codex import OpenAICodexModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai_harness import Coder
from pydantic_ai_harness.exa import ExaSearch
from pydantic_ai_harness.filesystem import FileSystem
from pydantic_ai_harness.planning import Planning
from pydantic_ai_harness.repo_context import RepoContext
from pydantic_ai_harness.shell import Shell
from pydantic_ai_harness.subagents import SubAgent

from pcode.planning import IdentifiedPlanning
from pcode.repo_context import AutomaticRepoContext


def create_coder(workspace: Path) -> CombinedCapability:
    """Keep repository context local, but allow file tools outside the workspace."""
    workspace = workspace.resolve()
    root = Path(workspace.anchor)
    path_guidance = (
        f"The working repository is {workspace}. File tools are rooted at {root}, "
        "not the repository: use absolute paths for file operations, including searches. "
        "Scope searches to the working repository unless the task needs another directory. "
    )
    explorer = Agent(
        name="explorer",
        description="Explore the codebase and answer questions without modifying anything",
        instructions=path_guidance + "Answer with concrete paths and evidence. "
        "Never read or print credential values or secret-bearing files.",
        capabilities=[
            FileSystem(root, read_only=True),
            AutomaticRepoContext(workspace_dir=workspace),
        ],
    )
    coder = Coder(workspace, subagents=[SubAgent(explorer)], instructions=path_guidance)
    # Supply discovery in each run's context, not as a model-driven tool call.
    coder.capabilities = [
        AutomaticRepoContext(workspace_dir=workspace)
        if isinstance(capability, RepoContext)
        else IdentifiedPlanning(
            **{
                field.name: getattr(capability, field.name)
                for field in fields(Planning)
                if field.init
            }
        )
        if isinstance(capability, Planning)
        else capability
        for capability in coder.capabilities
    ]
    # Coder has no separate filesystem-root option in Harness 0.31. Configure
    # its public FileSystem capability without moving Shell or RepoContext.
    for capability in coder.capabilities:
        if isinstance(capability, FileSystem):
            capability.root_dir = root
        elif isinstance(capability, Shell):
            # An empty allowlist alone can still leave Harness's default denylist.
            capability.allowed_commands = []
            capability.denied_commands = []
            capability.denied_operators = []
            capability.allow_interactive = True
    # Missing credentials must not prevent ordinary coding sessions. Let the
    # capability read the key itself; never put it in instructions or tool args.
    if os.environ.get("EXA_API_KEY", "").strip():
        coder.capabilities.append(ExaSearch())
    # Recompose so instruction sources track replaced/added capabilities too.
    return CombinedCapability(coder.capabilities)


def create_agent(model: str, workspace: Path) -> Agent:
    # Subscription endpoints reject the explicit cache markers that Harness
    # Planning adds after write_plan. Keep the native provider/auth/model name;
    # override only this advertised capability (verified against AI 2.43.0).
    resolved = (
        OpenAICodexModel(
            model.removeprefix("openai-codex:"),
            profile=OpenAIModelProfile(openai_supports_prompt_cache_breakpoints=False),
        )
        if model.startswith("openai-codex:")
        else model
    )
    return Agent(
        resolved,
        name="pcode",
        capabilities=[create_coder(workspace)],
        instructions=(
            "Answer repository questions using concrete file paths and evidence. "
            "Do not edit files or perform other mutations unless the user asks for them. "
            "Never read or print credential values: avoid .env, .envrc, credential files, "
            "private keys, token files, kubeconfigs, and secret-bearing tfvars. "
            "Inspect variable names only when needed. Do not expose secrets in tool calls."
        ),
    )
