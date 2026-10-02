"""The `claude` CLI binary pcode runs, and the environment it runs with."""

import sys
from pathlib import Path

# The child inherits pcode's environment. An empty value is unset to the CLI
# (verified: requests then bill the subscription), so neither pcode's own
# Anthropic key, token or endpoint nor a cloud route exported for other Claude
# Code use can silently redirect or bill a `claude:` request.
LOGIN_ENV = {
    "ANTHROPIC_API_KEY": "",
    "ANTHROPIC_AUTH_TOKEN": "",
    "ANTHROPIC_BASE_URL": "",
    "CLAUDE_CODE_USE_BEDROCK": "",
    "CLAUDE_CODE_USE_VERTEX": "",
    "CLAUDE_CODE_USE_FOUNDRY": "",
    "CLAUDE_CODE_USE_GATEWAY": "",
    "CLAUDE_CODE_USE_MANTLE": "",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS": "",
    "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD": "",
}
CLI_ENV = {
    **LOGIN_ENV,
    # pcode owns retries (visible in the UI), compaction and tool deferral. A
    # transcript the CLI compacted itself would no longer match pcode's history.
    "CLAUDE_CODE_MAX_RETRIES": "0",
    "DISABLE_AUTO_COMPACT": "1",
    "DISABLE_COMPACT": "1",
    "ENABLE_TOOL_SEARCH": "false",
    # pcode already bounds tool output; never let the CLI truncate it again.
    "MAX_MCP_OUTPUT_TOKENS": "1000000",
    # A subscription login defaults to 1-hour cache writes (2x input, against
    # 1.25x). pcode's tool loops send requests well inside five minutes, so the
    # hour rarely pays off (dev/anthropic-providers.md). This variable wins
    # over ENABLE_PROMPT_CACHING_1H; only FORCE_PROMPT_CACHING_5M outranks it.
    "CLAUDE_CODE_PROMPT_CACHE_TTL": "5m",
    # A parked handler lasts as long as the tool runs, delegations included.
    "MCP_TOOL_TIMEOUT": str(7 * 24 * 3600 * 1000),
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_AUTOUPDATER": "1",
}


def cli_path() -> str | None:
    """The CLI the SDK runs: its bundled binary, else `claude` on PATH."""
    import shutil

    import claude_agent_sdk

    name = "claude.exe" if sys.platform == "win32" else "claude"
    bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / name
    return str(bundled) if bundled.is_file() else shutil.which("claude")
