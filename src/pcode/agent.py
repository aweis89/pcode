"""Agent construction is independent of the terminal and runtime adapter."""

import os
import shutil
import sys
from dataclasses import fields, replace
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.capabilities import CombinedCapability
from pydantic_ai.models.openai_codex import OpenAICodexModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai_harness.coder import Coder
from pydantic_ai_harness.compaction import ClearToolResults, WarnNearLimits
from pydantic_ai_harness.exa import ExaSearch
from pydantic_ai_harness.filesystem import FileSystem
from pydantic_ai_harness.repo_context import RepoContext
from pydantic_ai_harness.shell import Shell
from pydantic_ai_harness.subagents import SubAgent, SubAgents

from pcode.cache_warnings import CacheBustReporting
from pcode.delegation import DelegationReporting, stream_child_activity
from pcode.filesystem import DisplayFileSystem
from pcode.llm_proxy import ProxiedCodexProvider
from pcode.meridian import MeridianSessionIdentity
from pcode.meridian_reminders import MeridianLimitWarnings
from pcode.output_limits import ModelOutputLimits
from pcode.planning import IdentifiedPlanning
from pcode.repo_context import create_repo_context
from pcode.usage_limits import UnlimitedRequests
from pcode.workspace_filesystem import WorkspaceFileSystem


def create_coder(workspace: Path) -> CombinedCapability:
    """Compose Harness's Coder with pcode's repository context and planning."""
    workspace = workspace.resolve()
    # uv tool entry points do not activate their environment's bin directory.
    # Append it only when rg is missing, preserving the user's command precedence.
    bundled_bin = Path(sys.executable).parent
    if shutil.which("rg") is None and (bundled_bin / "rg").is_file():
        os.environ["PATH"] = os.pathsep.join(
            part for part in (os.environ.get("PATH", ""), str(bundled_bin)) if part
        )
    coder = Coder(workspace)
    # Keep Coder's tool selection, including its persistent shell. File display
    # and repository discovery remain local adapters; planning is now opt-in.
    coder.capabilities = [
        create_repo_context(workspace)
        if isinstance(capability, RepoContext)
        else DisplayFileSystem.from_filesystem(capability)
        if isinstance(capability, FileSystem)
        else MeridianLimitWarnings(
            **{f.name: getattr(capability, f.name) for f in fields(capability) if f.init}
        )
        if isinstance(capability, WarnNearLimits)
        else capability
        for capability in coder.capabilities
    ]
    coder.capabilities.append(IdentifiedPlanning())
    coder.capabilities.append(DelegationReporting())
    coder.capabilities.append(MeridianSessionIdentity())
    coder.capabilities.append(ModelOutputLimits())
    coder.capabilities.append(CacheBustReporting())
    for capability in coder.capabilities:
        if isinstance(capability, Shell):
            # direnv writes its status banner to stderr on every cd into a
            # managed directory, which pollutes command output the agent parses
            # (e.g. `... | jq`). An empty log format silences it.
            capability.env = {**(capability.env or os.environ), "DIRENV_LOG_FORMAT": ""}
    parent_shell = next(c for c in coder.capabilities if isinstance(c, Shell))
    parent_files = next(c for c in coder.capabilities if isinstance(c, FileSystem))
    explorer = Agent(
        name="explorer",
        description=(
            "Explore files anywhere on the host and use shell commands for inspection "
            "and tests, without modifying the user's files or repository state"
        ),
        instructions=(
            "You are an explorer. Do not edit the user's files or modify repository state. "
            "You have read-only file tools and shell tools for inspection, Git queries, "
            "and safe tests. Do not use shell commands, scripts, redirects, or background "
            "processes to bypass the no-edit instruction. Avoid commands with destructive "
            "or persistent side effects; tests may create disposable test artifacts. "
            "Shell access is not sandboxed: this no-edit rule is an instruction, not an "
            "enforced permission boundary. Stop any background commands you start."
        ),
        capabilities=[
            UnlimitedRequests(),
            replace(WorkspaceFileSystem.from_filesystem(parent_files), read_only=True),
            replace(parent_shell),
            create_repo_context(workspace),
        ],
    )
    coder.capabilities.append(
        SubAgents(
            agents=[SubAgent(explorer)],
            agent_folders=None,
            event_stream_handler=stream_child_activity,
            shared_capabilities=[
                MeridianSessionIdentity(),
                ModelOutputLimits(),
                CacheBustReporting(),
            ],
        )
    )
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


# Pydantic AI 2.45.0 adds no `cache_control` of its own: without these settings an
# Anthropic conversation re-reads its whole prefix at full price every request
# (confirmed against captured request bodies and saved-session usage records).
# `anthropic_cache` is the server-side automatic breakpoint, which moves forward as
# history grows; the two explicit breakpoints keep instructions and tool definitions
# cached. Meridian is excluded on purpose: its passthrough proxy strips client
# `cache_control` and drives caching from its own lineage hash.
ANTHROPIC_CACHE_SETTINGS = {
    "anthropic_cache": "5m",
    "anthropic_cache_instructions": True,
    "anthropic_cache_tool_definitions": True,
}


def model_settings(model: str) -> dict | None:
    if model.startswith("openai-codex:"):
        # Codex does not emit visible reasoning unless summaries are requested.
        # Always receive them so Ctrl+T can reveal the preview mid-turn; the
        # display preference remains local and never changes reasoning effort.
        return {"openai_reasoning_summary": "detailed"}
    if model.startswith("anthropic:"):
        return dict(ANTHROPIC_CACHE_SETTINGS)
    return None


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
        from pcode.anthropic_oauth import anthropic_auth_source
        from pcode.auth import anthropic_model

        auth_source = anthropic_auth_source()
        if auth_source == "pi":
            from pcode.pi_auth import PiAnthropicModel

            # Explicit source selection: do not silently bill another credential.
            resolved = PiAnthropicModel(model)
        elif auth_source == "oauth":
            from pcode.anthropic_oauth import AnthropicOAuthModel

            resolved = AnthropicOAuthModel(model)
        elif auth_source == "api-key":
            key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
            if key:
                resolved = anthropic_model(model, key)
            else:
                # Allow the terminal to open so /login is reachable without credentials.
                defer_model_check = True
        else:
            raise ValueError("PCODE_ANTHROPIC_AUTH must be api-key, oauth, or pi.")
    return Agent(
        resolved,
        defer_model_check=defer_model_check,
        model_settings=model_settings(model),
        name="pcode",
        instructions=(
            "Responses are displayed in a terminal with Markdown rendering "
            "and syntax highlighting. "
            "Use fenced code blocks with a language tag for multiline code or shell examples, "
            "and inline backticks for identifiers and short commands. Close all code fences. "
            "Write ordinary prose outside code blocks. "
            # GPT-6 tends to edit through shell commands, bypassing captured edit diffs.
            "Prefer edit_file and write_file for file changes over shell tools."
        ),
        capabilities=[create_coder(workspace)],
    )
