"""Agent construction is independent of the terminal and runtime adapter."""

import os
import shutil
import sys
from collections.abc import Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from copy import copy
from dataclasses import fields, replace
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.capabilities import CombinedCapability
from pydantic_ai.models.openai_codex import OpenAICodexModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai_codex import OpenAICodexProvider
from pydantic_ai.toolsets import CombinedToolset
from pydantic_ai.usage import UsageLimits
from pydantic_ai_harness.coder import Coder
from pydantic_ai_harness.compaction import ClearToolResults, WarnNearLimits
from pydantic_ai_harness.filesystem import FileSystem
from pydantic_ai_harness.repo_context import RepoContext
from pydantic_ai_harness.shell import Shell
from pydantic_ai_harness.subagents import SubAgent, SubAgents
from pydantic_ai_harness.tool_output_limits import ToolOutputLimits

from pcode.cache_settings import ProviderCacheSettings, model_settings
from pcode.cache_warnings import CacheBustReporting
from pcode.code_mode import create_code_mode
from pcode.delegation import DelegationReporting, stream_child_activity
from pcode.ext import EXTENSION_GUIDE
from pcode.filesystem import DisplayFileSystem
from pcode.llm_proxy import ProxiedCodexProvider
from pcode.meridian import MeridianSessionIdentity
from pcode.meridian_reminders import MeridianLimitWarnings
from pcode.output_limits import ModelOutputLimits
from pcode.planning import IdentifiedPlanning
from pcode.preferences import SETTINGS, load_preferences
from pcode.repo_context import create_repo_context
from pcode.strict_tools import create_strict_tools
from pcode.tool_output_limits import create_tool_output_limits

# Generous enough for a real investigation, small enough that a child stuck in a
# loop is stopped within a turn rather than after a session's worth of requests.
# Harness isolates a child's request budget only when its `SubAgent` carries
# `usage_limits`; without one the child shares the parent's usage counter and
# silently gets the library's 50-request default, which a busy session has
# already spent. `pcode.ext.subagent` applies this to extension delegates too.
SUBAGENT_REQUEST_LIMIT = 120
SUBAGENT_TIMEOUT_SECONDS = 900

AGENT_INSTRUCTIONS = (
    "Responses are displayed in a terminal with Markdown rendering "
    "and syntax highlighting. "
    "Use fenced code blocks with a language tag for multiline code or shell examples, "
    "and inline backticks for identifiers and short commands. Close all code fences. "
    "Write ordinary prose outside code blocks. "
    # GPT-6 tends to edit through shell commands, bypassing captured edit diffs.
    "Prefer edit_file and write_file for file changes over shell tools. "
    # The model is the intended extension author, so it needs to know the
    # mechanism exists without the reference text sitting in every prompt.
    "pcode itself is extensible with small Python files (new slash commands, "
    "tools, guardrails on tool calls, extra instructions). When asked to change "
    f"how pcode behaves, first read {EXTENSION_GUIDE} and follow it."
)


_worker_toolsets: ContextVar[Sequence] = ContextVar("worker_toolsets", default=())


def worker_runtime_tools(ctx):
    return CombinedToolset(list(_worker_toolsets.get()))


@asynccontextmanager
async def worker_toolsets(toolsets: Sequence):
    """Give the built-in worker the same enabled runtime toolsets as its parent.

    Harness's inherit_tools covers constructor toolsets, not per-run MCP tools,
    and would also broaden specialized extension delegates. Context-local agent
    bindings keep this scoped to the worker and to this turn, including errors
    and cancellation; no stale MCP connection is retained after disable/reload.
    """
    token = _worker_toolsets.set(tuple(toolsets))
    try:
        yield
    finally:
        _worker_toolsets.reset(token)


def tool_retries() -> dict[str, int]:
    """The correction budget for tool-argument validation and `ModelRetry`.

    Pydantic AI defaults both tool and output retries to 1, so a second
    malformed call to a tool ends the whole turn with `UnexpectedModelBehavior`.
    A nested argument like `edit_file`'s `replacements` array is easy to mangle
    twice in a row, and a correction costs one round trip where the failure
    costs the turn. Output retries keep the stricter default: a model that
    cannot produce the final output shape twice is not going to converge.
    """
    configured = load_preferences().get("tool_retries", SETTINGS["tool_retries"].default)
    return {"tools": int(configured)}


def create_coder(
    workspace: Path, subagents: Sequence = (), extensions: Sequence = ()
) -> CombinedCapability:
    """Compose Harness's Coder with pcode's repository context and planning.

    `subagents` are extension-contributed Harness `SubAgent` entries, listed
    beside the worker under the one `delegate_task` tool.
    """
    workspace = workspace.resolve()
    # uv tool entry points do not activate their environment's bin directory.
    # Append it only when rg is missing, preserving the user's command precedence.
    bundled_bin = Path(sys.executable).parent
    if shutil.which("rg") is None and (bundled_bin / "rg").is_file():
        os.environ["PATH"] = os.pathsep.join(
            part for part in (os.environ.get("PATH", ""), str(bundled_bin)) if part
        )
    coder = Coder(workspace)
    output_limits = create_tool_output_limits()
    # Keep Coder's tool selection, including its persistent shell. File display
    # and repository discovery remain local adapters; planning is now opt-in.
    #
    # Ids must be set at construction (`replace(..., id=...)` or the factory):
    # a `Capability` binds its instructions to its id in `__init__`, so a later
    # `capability.id = ...` is silently ignored by /status attribution. Do not
    # blanket-rename everything either: `replace()`-copied children compare
    # fields with their parent and a renamed parent breaks that match.
    coder.capabilities = [
        create_repo_context(workspace)
        if isinstance(capability, RepoContext)
        # Named so /status can attribute its prompt; ids never reach the model.
        else replace(DisplayFileSystem.from_filesystem(capability), id="file_tools")
        if isinstance(capability, FileSystem)
        # Replace Coder's 64k truncation, so it cannot cut data before spilling.
        else output_limits
        if isinstance(capability, ToolOutputLimits)
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
    # Ahead of the worker copy below, so a delegate edits under the same schema.
    if strict_tools := create_strict_tools():
        coder.capabilities.append(strict_tools)
    debug = load_preferences().get("debug", SETTINGS["debug"].default) == "on"
    if debug:
        coder.capabilities.append(CacheBustReporting())
    for capability in coder.capabilities:
        if isinstance(capability, Shell):
            # direnv writes its status banner to stderr on every cd into a
            # managed directory, which pollutes command output the agent parses
            # (e.g. `... | jq`). An empty log format silences it.
            capability.env = {**(capability.env or os.environ), "DIRENV_LOG_FORMAT": ""}
    # Compose the worker from the same capabilities rather than maintaining a
    # second tool/policy list. Per-run capability state is still managed upstream.
    # These are supplied by SubAgents.shared_capabilities instead (also for
    # extension delegates); delegation itself is intentionally parent-only.
    shared_types = (
        ToolOutputLimits,
        MeridianSessionIdentity,
        ModelOutputLimits,
        CacheBustReporting,
    )
    worker_capabilities = [
        copy(capability)
        for capability in coder.capabilities
        if not isinstance(capability, (*shared_types, ClearToolResults, DelegationReporting))
    ]
    if code_mode := create_code_mode():
        coder.capabilities.append(code_mode)
        worker_capabilities.append(copy(code_mode))
    worker = Agent(
        name="worker",
        retries=tool_retries(),
        description=(
            "Complete a self-contained task using the main agent's tools and permissions, "
            "including file edits, shell commands, tests, web research, and enabled MCP tools"
        ),
        instructions=AGENT_INSTRUCTIONS
        + (
            " You are a general-purpose worker. Complete only the delegated task and report "
            "your changes, verification, and remaining limitations. You inherit the main "
            "agent's instructions, tools, and permission checks, but not its conversation. "
            "You share its workspace: coordinate edits with the parent. Your shell and "
            "plan are independent. You cannot delegate further. Stop background "
            "commands you no longer need."
        ),
        capabilities=[*worker_capabilities, *extensions],
        toolsets=[worker_runtime_tools],
    )
    coder.capabilities.append(
        SubAgents(
            agents=[
                SubAgent(
                    worker,
                    # An unattended child is the runaway worth bounding: its budget
                    # is its own, so exhausting it steers the parent with an
                    # observation instead of aborting the turn. Child usage is
                    # then isolated too, and rejoins session totals through
                    # `DelegationEndEvent.usage`.
                    usage_limits=UsageLimits(request_limit=SUBAGENT_REQUEST_LIMIT),
                    timeout_seconds=SUBAGENT_TIMEOUT_SECONDS,
                ),
                *subagents,
            ],
            agent_folders=None,
            event_stream_handler=stream_child_activity,
            shared_capabilities=[
                MeridianSessionIdentity(),
                ModelOutputLimits(),
                *([CacheBustReporting()] if debug else []),
                ProviderCacheSettings(),
                replace(output_limits),
            ],
        )
    )
    # Web search and fetch come from the bundled `web_research` extension, so a
    # user file of the same name can replace them.
    # Recompose so instruction sources track replaced/added capabilities too.
    # Summarize evidence before discarding it. Coder defaults to clearing old
    # tool results at 70%, which otherwise runs before pcode compaction.
    return CombinedCapability(
        [c for c in coder.capabilities if not isinstance(c, ClearToolResults)]
    )


def codex_model(model: str) -> OpenAICodexModel:
    """Build a Codex model on pcode's stored login, else the CLI's `auth.json`.

    Without a pcode store the provider reads the CLI file at construction time
    and keeps refreshed tokens in memory only.
    """
    from pcode.codex_login import credential_source

    proxy = os.environ.get("PCODE_LLM_PROXY", "").strip()
    source = credential_source()
    if proxy:
        provider = ProxiedCodexProvider(proxy, credential_source=source)
    elif source is not None:
        provider = OpenAICodexProvider(credential_source=source)
    else:
        provider = None
    # Subscription endpoints reject the explicit cache markers that Harness
    # Planning adds after write_plan. Keep the native provider/auth/model name;
    # override only this advertised capability (verified against AI 2.43.0).
    return OpenAICodexModel(
        model.removeprefix("openai-codex:"),
        profile=OpenAIModelProfile(openai_supports_prompt_cache_breakpoints=False),
        **({"provider": provider} if provider is not None else {}),
    )


def create_agent(
    model: str, workspace: Path, extensions: Sequence = (), subagents: Sequence = ()
) -> Agent:
    """Build the terminal's agent; `extensions` and `subagents` come from `pcode.ext`."""
    resolved = codex_model(model) if model.startswith("openai-codex:") else model
    if model.startswith("meridian:"):
        from pcode.meridian import meridian_model

        resolved = meridian_model(model)
    defer_model_check = False
    if model.startswith("anthropic:"):
        from pcode.anthropic_oauth import anthropic_auth_source
        from pcode.auth import anthropic_model

        auth_source = anthropic_auth_source()
        if auth_source == "oauth":
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
            raise ValueError("PCODE_ANTHROPIC_AUTH must be api-key or oauth.")
    return Agent(
        resolved,
        defer_model_check=defer_model_check,
        model_settings=model_settings(model),
        name="pcode",
        retries=tool_retries(),
        instructions=AGENT_INSTRUCTIONS,
        capabilities=[create_coder(workspace, subagents, extensions), *extensions],
    )
