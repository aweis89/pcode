import hashlib
import json
from io import StringIO
from types import SimpleNamespace

import httpx2
import pytest
from pydantic_ai.providers.openai_codex import OpenAICodexCredentials
from rich.console import Console

from pcode import usage

NOW = 1_000_000.0


def test_keychain_service_hashes_an_explicit_config_dir():
    # Claude Code suffixes the item whenever CLAUDE_CONFIG_DIR is set, even to ~/.claude.
    digest = hashlib.sha256(b"/home/me/.claude").hexdigest()[:8]
    assert usage.keychain_service("/home/me/.claude") == f"Claude Code-credentials-{digest}"
    assert usage.keychain_service(None) == "Claude Code-credentials"


def test_claude_login_falls_back_to_the_credentials_file(tmp_path, monkeypatch):
    monkeypatch.setattr(usage.sys, "platform", "linux")
    record = {"accessToken": "token", "subscriptionType": "max"}
    (tmp_path / ".credentials.json").write_text(json.dumps({"claudeAiOauth": record}))
    assert usage.claude_login(str(tmp_path)) == record
    with pytest.raises(usage.UsageUnavailable, match="No Claude Code login"):
        usage.claude_login(str(tmp_path / "missing"))


def test_claude_subscription_limits_and_disabled_extra_usage():
    data = {
        "limits": [
            {
                "kind": "session",
                "group": "session",
                "percent": 9,
                "resets_at": "1970-01-12T13:46:40+00:00",
            },
            {
                "kind": "weekly_scoped",
                "group": "weekly",
                "percent": 15,
                "resets_at": None,
                "scope": {"model": {"display_name": "Opus"}},
            },
        ],
        "spend": {
            "used": {"amount_minor": 0, "currency": "USD", "exponent": 2},
            "limit": {"amount_minor": 10000, "currency": "USD", "exponent": 2},
            "percent": 0,
            "enabled": False,
            "disabled_reason": "out_of_credits",
        },
    }
    assert usage.claude_lines(data, now=NOW - 3600) == [
        "Session (5h): 9% used, resets in 1h",
        "Weekly (Opus): 15% used",
        "Spend: $0.00 of $100.00 (0%), off (out of credits)",
    ]


def test_claude_enterprise_spend_cap():
    data = {
        "five_hour": None,
        "limits": [],
        "spend": {
            "used": {"amount_minor": 65242, "currency": "USD", "exponent": 2},
            "limit": {"amount_minor": 250000, "currency": "USD", "exponent": 2},
            "percent": 26,
            "enabled": True,
        },
    }
    assert usage.claude_lines(data) == ["Spend: $652.42 of $2,500.00 (26%)"]


def test_claude_windows_without_a_limits_list():
    data = {"five_hour": {"utilization": 6.0, "resets_at": None}, "spend": None}
    assert usage.claude_lines(data) == ["Session (5h): 6% used"]
    assert usage.claude_lines({}) == ["No limits reported for this login."]


def fake_claude(monkeypatch, response=None, **login):
    monkeypatch.setattr(usage, "claude_login", lambda: {"accessToken": "t", **login})
    monkeypatch.setattr("pcode.auth.oauth_user_agent", lambda: "claude-code/test")
    calls = []

    def get(url, **kwargs):
        calls.append(kwargs["headers"])
        return response

    monkeypatch.setattr(usage.httpx2, "get", get)
    return calls


def test_claude_usage_never_uses_an_expired_token(monkeypatch):
    calls = fake_claude(monkeypatch, expiresAt=1000)
    with pytest.raises(usage.UsageUnavailable, match="expired"):
        usage.claude_usage()
    assert calls == []


@pytest.mark.parametrize(
    ("status", "message"), [(401, "rejected"), (429, "rate limiting"), (500, "HTTP 500")]
)
def test_claude_usage_errors(monkeypatch, status, message):
    fake_claude(monkeypatch, httpx2.Response(status))
    with pytest.raises(usage.UsageUnavailable, match=message):
        usage.claude_usage()


def test_claude_usage_sends_claude_code_identity(monkeypatch):
    body = {"limits": [{"kind": "weekly_all", "percent": 66}]}
    calls = fake_claude(monkeypatch, httpx2.Response(200, json=body), subscriptionType="max")
    assert usage.claude_usage() == ["Plan: max", "Weekly: 66% used"]
    assert calls[0]["User-Agent"] == "claude-code/test"
    assert calls[0]["Authorization"] == "Bearer t"


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


def test_report_keeps_going_when_one_provider_fails(monkeypatch):
    def broken():
        raise usage.UsageUnavailable("No Claude Code login.")

    monkeypatch.setattr(usage, "claude_usage", broken)
    monkeypatch.setattr(usage, "codex_usage", lambda: ["Plan: plus"])
    assert usage.usage_report() == [
        "Claude Code",
        "  No Claude Code login.",
        "Codex",
        "  Plan: plus",
    ]


def test_usage_command_prints_the_report(monkeypatch):
    from pcode.app import PreviewApp

    monkeypatch.setattr(usage, "usage_report", lambda: ["Claude Code", "  Plan: max"])
    buffer = StringIO()
    runtime = SimpleNamespace(agent=SimpleNamespace(model=None), history=[])
    app = PreviewApp(model="test", runtime=runtime, console=Console(file=buffer))
    app.handle("/usage")
    assert "Plan: max" in buffer.getvalue()


def test_duration():
    assert [usage.duration(s) for s in (5, 90 * 60, 3 * 3600, 86400, 90000)] == [
        "1m",
        "1h 30m",
        "3h",
        "1d",
        "1d 1h",
    ]
