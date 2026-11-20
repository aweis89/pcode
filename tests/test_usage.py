import json
import stat
import subprocess
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
from pydantic_ai.providers.openai_codex import OpenAICodexCredentials
from rich.console import Console

from pcode import usage

NOW = 1_000_000.0

CLI_REPORT = (
    "You are currently using your subscription to power your Claude Code usage\n\n"
    "Current session: 6% used · resets Oct 16 at 10:30pm (America/Los_Angeles)\n"
    "Current week (all models): 9% used · resets Oct 19 at 6pm (America/Los_Angeles)\n\n"
    "What's contributing to your limits usage?\n"
    "Last 24h · 765 requests · 36 sessions\n"
)


def fake_cli(monkeypatch, stdout="", returncode=0, error=None, stderr=""):
    """Stand in for `claude -p /usage`; records the command and environment."""
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if error is not None:
            raise error
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)

    monkeypatch.setattr("pcode.models.claude_sdk_installed", lambda: True)
    monkeypatch.setattr("pcode.claude_sdk.cli_path", lambda: "/bin/claude")
    monkeypatch.setattr(usage.subprocess, "run", run)
    return calls


def cli_json(result, is_error=False):
    return json.dumps({"type": "result", "is_error": is_error, "num_turns": 0, "result": result})


def test_claude_usage_asks_the_cli_with_a_scrubbed_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-key")
    calls = fake_cli(monkeypatch, cli_json(CLI_REPORT))
    assert usage.claude_usage() == [
        "You are currently using your subscription to power your Claude Code usage",
        "Current session: 6% used · resets Oct 16 at 10:30pm (America/Los_Angeles)",
        "Current week (all models): 9% used · resets Oct 19 at 6pm (America/Los_Angeles)",
    ]
    ((args, kwargs),) = calls
    assert args[:3] == ["/bin/claude", "-p", "/usage"]
    assert "--no-session-persistence" in args
    # No user or project hooks run, and the CLI never waits on input.
    assert args[-2:] == ["--setting-sources", ""]
    assert kwargs["stdin"] is subprocess.DEVNULL
    # An API key must not answer for the subscription.
    assert kwargs["env"]["ANTHROPIC_API_KEY"] == ""


def test_claude_usage_without_a_subscription_login(monkeypatch):
    fake_cli(monkeypatch, cli_json("Total cost:            $0.0000\nUsage: 0 input, 0 output"))
    assert usage.claude_usage() == ["No Claude subscription login; run /login claude."]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"stdout": "not json", "returncode": 1}, "failed \\(exit 1\\)\\.$"),
        ({"returncode": 2, "stderr": "boom\nmore"}, "failed \\(exit 2\\): boom\\.$"),
        ({"stdout": cli_json("Not logged in\nmore", is_error=True)}, "^Not logged in$"),
        ({"error": subprocess.TimeoutExpired("claude", 30)}, "did not answer"),
        ({"error": FileNotFoundError()}, "Could not run Claude Code"),
    ],
)
def test_claude_usage_errors(monkeypatch, kwargs, message):
    fake_cli(monkeypatch, **kwargs)
    with pytest.raises(usage.UsageUnavailable, match=message):
        usage.claude_usage()


def test_claude_usage_needs_the_claude_extra(monkeypatch):
    monkeypatch.setattr("pcode.models.claude_sdk_installed", lambda: False)
    with pytest.raises(usage.UsageUnavailable, match="claude` extra"):
        usage.claude_usage()


CODEX_BODY = {
    "plan_type": "plus",
    "rate_limit": {
        "primary_window": {
            "used_percent": 12,
            "limit_window_seconds": 18000,
            "reset_at": NOW + 5400,
        },
        "secondary_window": {
            "used_percent": 40,
            "limit_window_seconds": 604800,
            "reset_at": NOW + 2 * 86400 + 3 * 3600,
        },
    },
    "additional_rate_limits": [
        {
            "limit_name": "Spark",
            "rate_limit": {"primary_window": {"used_percent": 3, "limit_window_seconds": 86400}},
        },
    ],
    "credits": {"has_credits": True, "unlimited": False, "balance": "42"},
    "spend_control": {
        "reached": False,
        "individual_limit": {"used": "10.00", "limit": "50.00", "used_percent": 20},
    },
}


def test_codex_lines():
    assert usage.codex_lines(CODEX_BODY, now=NOW) == [
        "Plan: plus",
        "Session (5h): 12% used, resets in 1h 30m",
        "Weekly: 40% used, resets in 2d 3h",
        "Spark 1d window: 3% used",
        "Credits: 42",
        "Spend: 10.00 of 50.00 (20%)",
    ]


class Store:
    async def load(self):
        return OpenAICodexCredentials(access_token="a", refresh_token="r", account_id="acct")

    async def save(self, credentials):
        raise AssertionError("no refresh expected")


def fake_codex(monkeypatch, handler, proxy=""):
    seen = {}

    # A subclass, since the provider type-checks the client it is given.
    class Client(httpx2.AsyncClient):
        def __init__(self, **kwargs):
            seen.update(kwargs)
            super().__init__(transport=httpx2.MockTransport(handler), timeout=kwargs["timeout"])

    monkeypatch.setattr(usage.httpx2, "AsyncClient", Client)
    monkeypatch.setattr("pcode.codex_login.credential_source", lambda: Store())
    monkeypatch.setenv("PCODE_LLM_PROXY", proxy)
    return seen


def test_codex_usage_uses_the_provider_auth_and_proxy(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx2.Response(200, json={"plan_type": "pro"})

    seen = fake_codex(monkeypatch, handler, proxy="http://proxy.test:8888")
    assert usage.codex_usage() == ["Plan: pro"]
    assert requests[0].headers["Authorization"] == "Bearer a"
    assert requests[0].headers["chatgpt-account-id"] == "acct"
    assert seen["proxy"] == "http://proxy.test:8888"
    assert seen["trust_env"] is False


def test_codex_usage_explains_a_bot_wall(monkeypatch):
    fake_codex(monkeypatch, lambda request: httpx2.Response(403, html="<html>"))
    with pytest.raises(usage.UsageUnavailable, match="set PCODE_LLM_PROXY"):
        usage.codex_usage()


def test_codex_usage_explains_an_unreachable_proxy(monkeypatch):
    def handler(request):
        raise httpx2.ConnectTimeout("timed out")

    fake_codex(monkeypatch, handler, proxy="http://proxy.test:8888")
    with pytest.raises(usage.UsageUnavailable, match="through PCODE_LLM_PROXY"):
        usage.codex_usage()


def _script(path: Path, body: str) -> str:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


ANTIGRAVITY_BODY = {
    "status": "SUCCESS",
    "response": "fallback text",
    "command": {
        "name": "usage",
        "data": {
            "groups": [
                {
                    "name": "Gemini Models",
                    "buckets": [
                        {
                            "id": "gemini-weekly",
                            "name": "Weekly Limit Remaining",
                            "window": "weekly",
                            "remaining_fraction": 0.94,
                            "reset_time": datetime.fromtimestamp(
                                NOW + 6 * 86400 + 20 * 3600, UTC
                            ).isoformat(),
                        },
                        {
                            "id": "gemini-5h",
                            "name": "Five Hour Limit Remaining",
                            "window": "5h",
                            "remaining_fraction": 0.80,
                            "reset_time": datetime.fromtimestamp(NOW + 2 * 3600, UTC).isoformat(),
                        },
                    ],
                },
                {
                    "name": "Claude and GPT models",
                    "buckets": [
                        {
                            "id": "3p-5h",
                            "name": "Five Hour Limit Remaining",
                            "window": "5h",
                            "remaining_fraction": 0.86,
                            "reset_time": datetime.fromtimestamp(
                                NOW + 2 * 3600 + 40 * 60, UTC
                            ).isoformat(),
                        },
                    ],
                },
            ]
        },
    },
}


def test_antigravity_lines():
    assert usage.antigravity_lines(ANTIGRAVITY_BODY, now=NOW) == [
        "Gemini Models (Weekly): 6% used, resets in 6d 20h",
        "Gemini Models (5h): 20% used, resets in 2h",
        "Claude and GPT models (5h): 14% used, resets in 2h 40m",
    ]
    assert usage.antigravity_lines({"response": "Line one\n\n  Line two  \n"}) == [
        "Line one",
        "Line two",
    ]
    assert usage.antigravity_lines({}) == ["No limits reported for this login."]


def test_antigravity_usage_runs_agy_with_scrubbed_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-key")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "synthetic-project")
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps(ANTIGRAVITY_BODY))
    agy = _script(
        tmp_path / "agy",
        'echo "[$GEMINI_API_KEY][$GOOGLE_CLOUD_PROJECT]|$*" > "$(dirname "$0")/call"\n'
        f'cat "{payload}"\n',
    )
    monkeypatch.setattr(usage.shutil, "which", lambda _: agy)

    lines = usage.antigravity_usage()
    assert lines[0].startswith("Gemini Models (Weekly): 6% used")
    assert lines[1].startswith("Gemini Models (5h): 20% used")
    assert (tmp_path / "call").read_text().strip() == "[][]|-p /usage --output-format json"


def test_antigravity_usage_bails_early_when_logged_out(monkeypatch, tmp_path):
    # Simulates `agy -p /usage` printing an auth prompt on stderr and waiting 60s.
    agy = _script(
        tmp_path / "agy",
        'echo "Authentication required. Please visit the URL to log in:" >&2\nexec sleep 30\n',
    )
    monkeypatch.setattr(usage.shutil, "which", lambda _: agy)
    monkeypatch.setattr(usage, "ANTIGRAVITY_USAGE_TIMEOUT", 10)

    with pytest.raises(
        usage.UsageUnavailable, match="No Antigravity login; run /login antigravity"
    ):
        usage.antigravity_usage()


def test_antigravity_usage_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(usage.shutil, "which", lambda _: None)
    with pytest.raises(usage.UsageUnavailable, match="was not found"):
        usage.antigravity_usage()

    bad_exit = _script(tmp_path / "agy-fail", 'echo "boom" >&2\nexit 2\n')
    monkeypatch.setattr(usage.shutil, "which", lambda _: bad_exit)
    with pytest.raises(usage.UsageUnavailable, match="failed \\(exit 2\\): boom"):
        usage.antigravity_usage()

    bad_status = _script(tmp_path / "agy-status", 'echo \'{"status":"ERROR"}\'\n')
    monkeypatch.setattr(usage.shutil, "which", lambda _: bad_status)
    with pytest.raises(usage.UsageUnavailable, match="failed \\(ERROR\\)"):
        usage.antigravity_usage()

    slow = _script(tmp_path / "agy-slow", "exec sleep 30\n")
    monkeypatch.setattr(usage.shutil, "which", lambda _: slow)
    monkeypatch.setattr(usage, "ANTIGRAVITY_USAGE_TIMEOUT", 0.2)
    with pytest.raises(usage.UsageUnavailable, match="did not answer in time"):
        usage.antigravity_usage()


def test_report_keeps_going_when_one_provider_fails(monkeypatch):
    def broken():
        raise usage.UsageUnavailable("No Claude Code login.")

    monkeypatch.setattr(usage, "claude_usage", broken)
    monkeypatch.setattr(usage, "codex_usage", lambda: ["Plan: plus"])
    monkeypatch.setattr(usage, "antigravity_usage", lambda: ["Gemini Models (5h): 10% used"])
    assert usage.usage_report() == [
        "Claude Code",
        "  No Claude Code login.",
        "Codex",
        "  Plan: plus",
        "Antigravity",
        "  Gemini Models (5h): 10% used",
    ]


def test_report_for_single_provider_does_not_query_others(monkeypatch):
    called = []

    def claude():
        called.append("claude")
        return ["Plan: max"]

    def codex():
        called.append("codex")
        return ["Plan: plus"]

    def agy():
        called.append("antigravity")
        return ["Gemini Models (5h): 10% used"]

    monkeypatch.setattr(usage, "claude_usage", claude)
    monkeypatch.setattr(usage, "codex_usage", codex)
    monkeypatch.setattr(usage, "antigravity_usage", agy)

    assert usage.usage_report("antigravity") == [
        "Antigravity",
        "  Gemini Models (5h): 10% used",
    ]
    assert called == ["antigravity"]


def test_usage_command_defaults_to_current_provider_and_accepts_arguments(monkeypatch):
    from pcode.app import PreviewApp

    seen = []

    def fake_report(provider=None):
        seen.append(provider)
        return [f"Report:{provider or 'all'}"]

    monkeypatch.setattr(usage, "usage_report", fake_report)

    for model, command, expected in (
        ("antigravity:gemini-4-neon-high", "/usage", "antigravity"),
        ("claude:claude-sonnet-5", "/usage", "claude"),
        ("openai-codex:gpt-5", "/usage", "openai-codex"),
        ("anthropic:claude-sonnet-5", "/usage", None),
        ("antigravity:gemini-4-neon-high", "/usage all", None),
        ("antigravity:gemini-4-neon-high", "/usage claude", "claude"),
    ):
        buffer = StringIO()
        runtime = SimpleNamespace(agent=SimpleNamespace(model=None), history=[])
        app = PreviewApp(model=model, runtime=runtime, console=Console(file=buffer))
        app.handle(command)
        assert f"Report:{expected or 'all'}" in buffer.getvalue()

    assert seen == ["antigravity", "claude", "openai-codex", None, None, "claude"]


def test_duration():
    assert [usage.duration(s) for s in (5, 90 * 60, 3 * 3600, 86400, 90000)] == [
        "1m",
        "1h 30m",
        "3h",
        "1d",
        "1d 1h",
    ]
