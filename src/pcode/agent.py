"""Agent construction is independent of the terminal and runtime adapter."""

from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.models.openai_codex import OpenAICodexModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai_harness import Coder


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
        capabilities=[Coder(workspace.resolve())],
        instructions=(
            "Answer repository questions using concrete file paths and evidence. "
            "Do not edit files or perform other mutations unless the user asks for them. "
            "Never read or print credential values: avoid .env, .envrc, credential files, "
            "private keys, token files, kubeconfigs, and secret-bearing tfvars. "
            "Inspect variable names only when needed. Do not expose secrets in tool calls."
        ),
    )
