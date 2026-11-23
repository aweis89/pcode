"""Local model suggestions for configured providers; no credential or network reads."""

import importlib.util
import os
import re
import shutil
from pathlib import Path
from typing import get_args

# Every prefix the picker accepts, with a display label. Each provider's SDK
# extra must be installed (see pyproject.toml) or infer_model raises on switch.
PROVIDERS = {
    "alibaba": "Alibaba",
    "anthropic": "Anthropic",
    "antigravity": "Antigravity",
    "azure": "Azure OpenAI",
    "bedrock": "Bedrock",
    "bedrock-mantle": "Bedrock Mantle",
    "cerebras": "Cerebras",
    "claude": "Claude Code",
    "crusoe": "Crusoe",
    "deepseek": "DeepSeek",
    "fireworks": "Fireworks",
    "github-copilot": "GitHub Copilot",
    "google": "Google",
    "google-cloud": "Google Cloud",
    "groq": "Groq",
    "heroku": "Heroku",
    "moonshotai": "Moonshot",
    "nebius": "Nebius",
    "ollama": "Ollama",
    "openai": "OpenAI",
    "openai-chat": "OpenAI (chat)",
    "openai-codex": "OpenAI Codex",
    "openai-responses": "OpenAI (responses)",
    "openrouter": "OpenRouter",
    "ovhcloud": "OVHcloud",
    "sambanova": "SambaNova",
    "snowflake": "Snowflake",
    "together": "Together",
    "vercel": "Vercel",
    "vllm": "vLLM",
    "xai": "xAI",
    "zai": "Z.ai",
}

# Providers activated by environment. Each inner tuple is one requirement
# satisfied by any listed variable; every requirement must hold. Only names
# are inspected, never values. Providers needing an endpoint as well as a key
# (azure, github-copilot) list both so a lone key is not a false positive.
ENV_PROVIDERS: dict[str, tuple[tuple[str, ...], ...]] = {
    "alibaba": (("ALIBABA_API_KEY", "DASHSCOPE_API_KEY"),),
    "azure": (("AZURE_OPENAI_API_KEY",), ("AZURE_OPENAI_ENDPOINT",)),
    "bedrock": (("AWS_BEARER_TOKEN_BEDROCK", "AWS_ACCESS_KEY_ID", "AWS_PROFILE"),),
    "bedrock-mantle": (("AWS_BEARER_TOKEN_BEDROCK",),),
    "cerebras": (("CEREBRAS_API_KEY",),),
    "crusoe": (("CRUSOE_API_KEY",),),
    "deepseek": (("DEEPSEEK_API_KEY",),),
    "fireworks": (("FIREWORKS_API_KEY",),),
    "github-copilot": (
        ("GITHUB_COPILOT_API_KEY", "GITHUB_COPILOT_API_TOKEN", "COPILOT_GITHUB_TOKEN"),
        ("GITHUB_COPILOT_BASE_URL", "GITHUB_COPILOT_API_BASE", "COPILOT_API_URL"),
    ),
    "google": (("GOOGLE_API_KEY", "GEMINI_API_KEY"),),
    "google-cloud": (("GOOGLE_CLOUD_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS"),),
    "groq": (("GROQ_API_KEY",),),
    "heroku": (("HEROKU_INFERENCE_KEY",),),
    "moonshotai": (("MOONSHOTAI_API_KEY",),),
    "nebius": (("NEBIUS_API_KEY",),),
    "ollama": (("OLLAMA_BASE_URL",),),
    "openai": (("OPENAI_API_KEY",),),
    "openrouter": (("OPENROUTER_API_KEY",),),
    "ovhcloud": (("OVHCLOUD_API_KEY",),),
    "sambanova": (("SAMBANOVA_API_KEY",),),
    "snowflake": (("SNOWFLAKE_TOKEN",), ("SNOWFLAKE_ACCOUNT",)),
    "together": (("TOGETHER_API_KEY",),),
    "vercel": (("VERCEL_AI_GATEWAY_API_KEY", "VERCEL_OIDC_TOKEN"),),
    "vllm": (("VLLM_BASE_URL",),),
    "xai": (("XAI_API_KEY",),),
    "zai": (("ZAI_API_KEY",),),
}


def runs_own_loop(model) -> bool:
    """Whether `model`'s own CLI runs the agent loop, with its tools, context and titles.

    True for `antigravity:` (see `pcode.antigravity`): pcode's tools, plan
    reminders and summaries don't apply to it. `model` is a model object or a
    `provider:name` string.
    """
    if isinstance(model, str):
        return model.strip().lower().startswith("antigravity:")
    return getattr(model, "system", None) == "antigravity"


def login_sources() -> tuple[str, ...]:
    """What `/login` accepts; bare `/login` prefers the current model's provider, else the first."""
    return ("claude", "openai-codex", "antigravity")


def logout_sources() -> tuple[str, ...]:
    """What `/logout` offers; the first is what a bare `/logout` removes."""
    return ("openai-codex",)


def anthropic_credential_hint() -> str:
    """How to give an `anthropic:` model that has no credential one."""
    return "Set ANTHROPIC_API_KEY, or switch to a claude: model."


# Catalog prefix -> KnownModelName prefixes whose IDs it accepts. Codex has no
# separate SDK catalog, so expose every OpenAI model ID and let the provider
# enforce account access; Claude Code fronts Anthropic models.
CATALOG_SOURCES = {"openai-codex": "openai", "claude": "anthropic"}
# Released models Pydantic AI's KnownModelName doesn't list yet, by catalog
# source. Drop an entry once the locked release lists it.
UNLISTED_MODELS: dict[str, tuple[str, ...]] = {}


def claude_code_configured() -> bool:
    """Whether this machine has used Claude Code, whose login `claude:` models share.

    The Agent SDK bundles the CLI, so an install is not required; its config
    is the signal, checked for existence only.
    """
    config = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    try:
        return bool(
            (Path(config).expanduser() if config else Path.home() / ".claude").is_dir()
            or (Path.home() / ".claude.json").is_file()
            or shutil.which("claude")
        )
    except OSError:
        return False


def claude_sdk_installed() -> bool:
    """Whether pcode was installed with its `claude` extra (claude-agent-sdk)."""
    return importlib.util.find_spec("claude_agent_sdk") is not None


def _configured(requirements: tuple[tuple[str, ...], ...]) -> bool:
    return all(any(os.environ.get(name, "").strip() for name in group) for group in requirements)


def active_providers(current: str | None) -> set[str]:
    active = set()
    if current and current.partition(":")[0] in PROVIDERS:
        active.add(current.partition(":")[0])
    if claude_sdk_installed() and claude_code_configured():
        active.add("claude")
    if shutil.which("agy"):
        active.add("antigravity")
    active.update(name for name, needs in ENV_PROVIDERS.items() if _configured(needs))
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        active.add("anthropic")
    from pcode.codex_login import have_credentials

    # Only check existence, never read a credential to populate the picker.
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    directory = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    try:
        if have_credentials() or (directory / "auth.json").is_file():
            active.add("openai-codex")
    except OSError:
        pass
    from pcode.preferences import load_preferences

    allowed = load_preferences().get("model_providers", "")
    if allowed:
        active.intersection_update(name.strip() for name in allowed.split(","))
    return active


def model_catalog(providers: set[str], current: str | None = None) -> list[str]:
    # Imported here so the terminal can read this module's settings without
    # loading the agent stack. KnownModelName is a TypeAliasType on supported
    # Pydantic versions.
    from pydantic_ai.models import KnownModelName

    known = get_args(getattr(KnownModelName, "__value__", KnownModelName))
    by_source: dict[str, set[str]] = {}
    for name in known:
        if isinstance(name, str):
            source, _, model = name.partition(":")
            by_source.setdefault(source, set()).add(model)
    for source, extra in UNLISTED_MODELS.items():
        by_source.setdefault(source, set()).update(extra)
    models = set()
    if "antigravity" in providers:
        from pcode.antigravity import cached_models

        # What the account offered last time; the default until a session has run.
        offered = cached_models() or ("gemini-4-neon",)
        models.update(f"antigravity:{name}" for name in offered)
    for provider in providers:
        source = CATALOG_SOURCES.get(provider, provider)
        # Providers without a catalog (openrouter, ollama, vllm...) take any
        # model ID typed into the picker instead.
        models.update(f"{provider}:{model}" for model in by_source.get(source, ()))
    if current and current.partition(":")[0] in providers:
        models.add(current)
    from pcode.preferences import recent_providers

    recent = {name: rank for rank, name in enumerate(recent_providers())}
    unused = len(recent)
    # Recently used providers first, the rest alphabetically. The picker filters
    # without re-ranking, so this also decides which provider's copy of a model
    # a typed filter offers first.
    return sorted(
        models, key=lambda name: (recent.get(name.partition(":")[0], unused), model_sort_key(name))
    )


def session_provider(model) -> str:
    """The pcode provider of a session's model: a `provider:name` string or a model object.

    A model object's `system` is its provider's name, which matches the prefix
    it was chosen under for every provider bare names can resolve against.
    "" when there is no model or its provider is not one pcode knows.
    """
    if model is None:
        return ""
    provider = (
        model.partition(":")[0]
        if isinstance(model, str)
        else str(getattr(model, "system", "") or "")
    )
    return provider if provider in PROVIDERS else ""


def has_provider(name: str) -> bool:
    """Whether `name` names its provider, as in `claude:claude-opus-5-5`.

    Model ids can hold `:` themselves (`llama3:8b`), so only a known provider
    before the first one counts.
    """
    return name.partition(":")[0] in PROVIDERS


def provider_models(provider: str) -> list[str]:
    """The catalog's names under `provider`, without the prefix; [] when it has no catalog."""
    prefix = f"{provider}:"
    return [
        name.removeprefix(prefix)
        for name in sorted(model_catalog({provider}), key=model_sort_key)
        if name.startswith(prefix)
    ]


def qualify_model(name: str, session=None) -> str:
    """`name` with the session's provider when it was given bare, else as given.

    A name that names its provider is never rewritten. A bare one becomes
    `<session provider>:<name>` when the catalog lists it there, when it is
    the session's own model, or when that provider has no catalog and takes
    any id (openrouter, ollama...). Anything
    else comes back unchanged for the usual inference to try. Reads no
    credentials, so it is safe before a model is resolved. `session` is the
    session's model, as for `session_provider`.
    """
    name = name.strip()
    if not name or has_provider(name):
        return name
    provider = session_provider(session)
    if not provider:
        return name
    qualified = f"{provider}:{name}"
    if qualified in (session, getattr(session, "model_id", None)):
        return qualified
    known = provider_models(provider)
    return qualified if not known or name in known else name


def model_sort_key(name: str):
    """Group providers/families alphabetically, then numeric versions newest first.

    Compare numeric components as integers so 4.10 precedes 4.9. Undated aliases
    sort before dated snapshots of the same version. Do not pin the current model
    above newer suggestions; the picker marks it separately.
    """
    provider, _, model = name.partition(":")
    parts = tuple(
        (1, -int(part)) if part.isdigit() else (0, part.casefold())
        for part in re.split(r"(\d+)", model)
    )
    return provider, parts


# A `-2024-08-06` date; other snapshot suffixes are caught per segment below.
_ISO_DATE = re.compile(r"[-_.]\d{4}-\d{2}-\d{2}(?=$|[-_./])")
# Numbers after these are preview dates (`preview-09-2025`), not versions.
_DATED_TAGS = {"preview", "exp"}


def _family_and_version(name: str) -> tuple[tuple, tuple[int, ...], bool]:
    """`claude-opus-4-5` -> family (provider, claude, opus), version (4, 5), not a snapshot.

    Only standalone numbers are versions: `ministral-14b` and `gpt-4o` keep their
    digits as part of the name, since those are sizes or variants, not releases.
    Snapshot dates (`-20250514`, `-2411`, `-2024-08-06`, `preview-05-20`) are
    neither, so a snapshot ties with its alias on version.
    """
    provider, _, model = name.casefold().partition(":")
    undated = _ISO_DATE.sub("", model)
    snapshot = undated != model
    family, version, dated = [], [], False
    for part in re.split(r"[-_./]", undated):
        if part.isdigit():
            # No release number runs to four digits; a snapshot date does.
            if not dated and len(part) < 4:
                version.append(int(part))
            else:
                snapshot = True
        elif part and part != "latest":
            family.append(part)
            dated = dated or part in _DATED_TAGS
    return (provider, *family), tuple(version), snapshot


def latest_models(names: list[str], keep: str | None = None) -> list[str]:
    """The newest version of each model family in `names`, in their original order.

    `claude-opus-5-5` drops `claude-opus-4-8` and dated snapshots of either; an
    undated alias beats a snapshot of the same version. `keep` (such as the
    session's own model) stays even when it is not the newest.
    """
    best: dict[tuple, tuple] = {}
    for name in names:
        family, version, snapshot = _family_and_version(name)
        candidate = (version, not snapshot, -len(name), name)
        if family not in best or candidate[:3] > best[family][:3]:
            best[family] = candidate
    chosen = {entry[-1] for entry in best.values()} | {keep}
    return [name for name in names if name in chosen]
