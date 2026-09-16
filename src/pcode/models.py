"""Local model suggestions for configured providers; no credential or network reads."""

import os
import re
import shutil
from pathlib import Path
from typing import get_args

from pydantic_ai.models import KnownModelName

PROVIDERS = {"anthropic": "Anthropic", "openai-codex": "OpenAI Codex", "meridian": "Meridian"}


def active_providers(current: str | None) -> set[str]:
    active = set()
    if current and current.partition(":")[0] in PROVIDERS:
        active.add(current.partition(":")[0])
    if os.environ.get("PCODE_MERIDIAN_BASE_URL", "").strip() or shutil.which("meridian"):
        active.add("meridian")
    source = os.environ.get("PCODE_ANTHROPIC_AUTH", "api-key").strip()
    if source == "pi" or (
        source in {"", "api-key"} and os.environ.get("ANTHROPIC_API_KEY", "").strip()
    ):
        active.add("anthropic")
    # Only check existence, never read a credential to populate the picker.
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    directory = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    try:
        if (directory / "auth.json").is_file():
            active.add("openai-codex")
    except OSError:
        pass
    if os.environ.get("PCODE_LLM_PROXY", "").strip():
        active.intersection_update({"openai-codex"})
    return active


def model_catalog(providers: set[str], current: str | None = None) -> list[str]:
    # KnownModelName is a TypeAliasType on supported Pydantic versions.
    known = get_args(getattr(KnownModelName, "__value__", KnownModelName))
    models = set()
    for name in known:
        if not isinstance(name, str):
            continue
        provider, _, model = name.partition(":")
        if provider == "anthropic" and provider in providers:
            models.add(name)
        if provider == "anthropic" and "meridian" in providers:
            models.add(f"meridian:{model}")
        # Codex accepts OpenAI model IDs, but has no separate SDK catalog. Suggest
        # GPT-5 base/coding variants, not dated, ChatGPT, pro, audio, or nano IDs.
        if (
            "openai-codex" in providers
            and provider == "openai"
            and re.fullmatch(r"gpt-5(?:\.\d+)?(?:-codex(?:-max)?|-luna|-sol|-terra|-mini)?", model)
        ):
            models.add(f"openai-codex:{model}")
    if current and current.partition(":")[0] in providers:
        models.add(current)
    return sorted(models, key=lambda name: (name != current, name))
