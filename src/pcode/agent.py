"""Agent construction is independent of the terminal and runtime adapter."""

from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai_harness import Coder


def create_agent(model: str, workspace: Path) -> Agent:
    return Agent(
        model,
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
