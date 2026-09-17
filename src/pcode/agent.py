"""Agent construction is independent of the terminal and runtime adapter."""

import os
from dataclasses import fields
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.capabilities import CombinedCapability
from pydantic_ai.models.openai_codex import OpenAICodexModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai_harness import Coder
from pydantic_ai_harness.compaction import ClearToolResults
from pydantic_ai_harness.exa import ExaSearch
from pydantic_ai_harness.filesystem import FileSystem
from pydantic_ai_harness.planning import Planning
from pydantic_ai_harness.repo_context import RepoContext
from pydantic_ai_harness.shell import Shell
from pydantic_ai_harness.subagents import SubAgent, SubAgents

from pcode.delegation import DelegationReporting, stream_child_activity
from pcode.llm_proxy import ProxiedCodexProvider
from pcode.meridian import MeridianSessionIdentity
from pcode.output_limits import ModelOutputLimits
from pcode.planning import IdentifiedPlanning
from pcode.repo_context import AutomaticRepoContext
from pcode.usage_limits import UnlimitedRequests


def create_coder(workspace: Path) -> CombinedCapability:
    """Compose Harness's Coder with pcode's repository context and planning."""
    workspace = workspace.resolve()
    explorer = Agent(
        name="explorer",
        description="Explore the codebase and answer questions without modifying anything",
        capabilities=[
            UnlimitedRequests(),
            FileSystem(workspace, read_only=True),
            AutomaticRepoContext(workspace_dir=workspace),
        ],
    )
    coder = Coder(workspace, subagents=[SubAgent(explorer)])
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
    coder.capabilities.append(DelegationReporting())
    coder.capabilities.append(MeridianSessionIdentity())
    coder.capabilities.append(ModelOutputLimits())
    for capability in coder.capabilities:
        if isinstance(capability, SubAgents):
            capability.event_stream_handler = stream_child_activity
            capability.shared_capabilities = [
                *capability.shared_capabilities,
                MeridianSessionIdentity(),
                ModelOutputLimits(),
            ]
        if isinstance(capability, Shell):
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
    # Summarize evidence before discarding it. Coder defaults to clearing old
    # tool results at 70%, which otherwise runs before pcode compaction.
    return CombinedCapability(
        [c for c in coder.capabilities if not isinstance(c, ClearToolResults)]
    )


def create_agent(model: str, workspace: Path) -> Agent:
    proxy = os.environ.get("PCODE_LLM_PROXY", "").strip()
    # Subscription endpoints reject the explicit cache markers that Harness
    # Planning adds after write_plan. Keep the native provider/auth/model name;
    # override only this advertised capability (verified against AI 2.43.0).
    resolved = (
        OpenAICodexModel(
            model.removeprefix("openai-codex:"),
            profile=OpenAIModelProfile(openai_supports_prompt_cache_breakpoints=False),
            **({"provider": ProxiedCodexProvider(proxy)} if proxy else {}),
        )
        if model.startswith("openai-codex:")
        else model
    )
    if model.startswith("meridian:"):
        from pcode.meridian import meridian_model

        resolved = meridian_model(model)
    defer_model_check = False
    if model.startswith("anthropic:"):
        from pcode.auth import anthropic_model

        auth_source = os.environ.get("PCODE_ANTHROPIC_AUTH", "api-key").strip()
        if auth_source == "pi":
            from pcode.pi_auth import PiAnthropicModel

            # Explicit source selection: do not silently bill another credential.
            resolved = PiAnthropicModel(model)
        elif auth_source in {"", "api-key"}:
            key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
            if key:
                resolved = anthropic_model(model, key)
            else:
                # Allow the terminal to open so /login is reachable without credentials.
                defer_model_check = True
        else:
            raise ValueError("PCODE_ANTHROPIC_AUTH must be api-key or pi.")
    return Agent(
        resolved,
        defer_model_check=defer_model_check,
        # Codex does not emit visible reasoning unless summaries are requested.
        # Always receive them so Ctrl+T can reveal the preview mid-turn; the
        # display preference remains local and never changes reasoning effort.
        model_settings=(
            {"openai_reasoning_summary": "auto"} if model.startswith("openai-codex:") else None
        ),
        name="pcode",
        instructions=(
            "Responses are displayed in a terminal with Markdown rendering "
            "and syntax highlighting. "
            "Use fenced code blocks with a language tag for multiline code or shell examples, "
            "and inline backticks for identifiers and short commands. Close all code fences. "
            "Write ordinary prose outside code blocks."
        ),
        capabilities=[create_coder(workspace)],
    )
