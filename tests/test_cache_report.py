"""The cache report must separate healthy sessions from the regression it exists to catch."""

import asyncio
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.usage import RequestUsage
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, SqliteStepStore

from pcode.sessions import SessionInfo

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "cache_report.py"
_spec = importlib.util.spec_from_file_location("cache_report", SCRIPT)
cache_report = importlib.util.module_from_spec(_spec)
# `@dataclass` resolves its module through `sys.modules`, so a script loaded by
# path must be registered there before it is executed.
sys.modules["cache_report"] = cache_report
_spec.loader.exec_module(cache_report)


def usage(read: int, write: int, total: int) -> RequestUsage:
    """Mirror how the Anthropic adapter records a request's cache verdict."""
    return RequestUsage(
        input_tokens=total,
        output_tokens=10,
        cache_read_tokens=read,
        cache_write_tokens=write,
        details={"input_tokens": 2},
    )


def write_info(directory: Path, identity: str, model: str = "anthropic:claude-opus-5") -> None:
    """Write real session metadata: the reader validates it before listing."""
    info = SessionInfo(
        id=identity,
        model=model,
        workspace=str(directory),
        created="2026-01-01T00:00:00+00:00",
        updated="2026-01-01T00:00:00+00:00",
        packages={},
    )
    (directory / "session.json").write_text(info.model_dump_json())


def response(index: int, read: int, write: int, total: int) -> ModelResponse:
    return ModelResponse(
        parts=[ToolCallPart(tool_name="probe", args={}, tool_call_id=f"call_{index}")],
        usage=usage(read, write, total),
        model_name="claude-opus-5",
        provider_response_id=f"msg_{index}",
        timestamp=datetime(2026, 1, 1, 0, index // 60, index % 60, tzinfo=timezone.utc),
    )


def write_session(
    root: Path, identity: str, *, healthy: bool, requests: int = 12, reminders: tuple[str, ...] = ()
) -> Path:
    """Build a session through the real store, so the schema is never assumed.

    A healthy session's reads track the previous request's input; the regression
    pins reads to the first prefix and rewrites the growing tail every request.
    """
    directory = root / identity
    directory.mkdir(parents=True)
    write_info(directory, identity)
    store = SqliteStepStore(database=directory / "steps.sqlite3")
    history: list = [ModelRequest(parts=[UserPromptPart(content="start")])]
    for reminder in reminders:
        history.append(ModelRequest(parts=[UserPromptPart(content=reminder)]))

    async def build() -> None:
        total = 4000
        pinned = total
        for index in range(requests):
            read = total if healthy else pinned
            write = 300 if healthy else total - pinned
            total += 300
            history.append(response(index, read=read if index else 0, write=write, total=total))
            history.append(
                ModelRequest(
                    parts=[
                        ToolReturnPart(tool_name="probe", content="r", tool_call_id=f"call_{index}")
                    ]
                )
            )
            await store.save_snapshot(
                ContinuableSnapshot(run_id="run", step_index=index, messages=list(history))
            )
        history.append(ModelResponse(parts=[TextPart("done")], model_name="claude-opus-5"))
        await store.save_snapshot(
            ContinuableSnapshot(run_id="run", step_index=requests, messages=list(history))
        )

    asyncio.run(build())
    return directory


def test_healthy_session_passes_and_regression_fails(tmp_path):
    healthy = cache_report.analyze(write_session(tmp_path, "aaaaaaa1", healthy=True))
    regressed = cache_report.analyze(write_session(tmp_path, "bbbbbbb2", healthy=False))

    assert [f.level for f in healthy.findings] == ["ok"]
    assert healthy.read_share > 0.85
    assert not healthy.prefix_rewrites

    levels = [f.level for f in regressed.findings]
    assert levels.count("fail") == 2, regressed.findings
    assert "pinned" in " ".join(f.message for f in regressed.findings)
    assert regressed.read_share < healthy.read_share


def test_duplicate_plan_reminders_are_reported(tmp_path):
    tag = cache_report.PLAN_TAG
    directory = write_session(
        tmp_path,
        "ccccccc3",
        healthy=True,
        reminders=(f"{tag}\nA\n", f"{tag}\nB\n", f"{tag}\nA\n"),
    )
    report = cache_report.analyze(directory)
    assert len(report.reminders) == 3
    assert [f.message for f in report.findings if f.level == "warn"] == [
        "1 duplicate plan reminder(s) of 3 in the final history: deduplication is not "
        "matching previously sent text."
    ]


def test_short_session_is_not_reported_as_a_regression(tmp_path):
    """Below the provider's minimum cacheable size, zero reads is expected."""
    directory = tmp_path / "eeeeeee5"
    directory.mkdir()
    write_info(directory, "eeeeeee5", model="test")
    store = SqliteStepStore(database=directory / "steps.sqlite3")
    messages = [
        ModelRequest(parts=[UserPromptPart(content="hi")]),
        response(0, read=0, write=0, total=100),
    ]

    asyncio.run(
        store.save_snapshot(ContinuableSnapshot(run_id="run", step_index=0, messages=messages))
    )
    report = cache_report.analyze(directory)
    assert [f.level for f in report.findings] == ["info"]
    assert "nothing it was obliged to cache" in report.findings[0].message


def test_report_never_prints_message_content(tmp_path, capsys):
    """Prompts hold file contents and command output; only counts may be shown."""
    secret = "SECRET-PROMPT-TEXT"
    directory = write_session(
        tmp_path, "ddddddd4", healthy=True, reminders=(f"{cache_report.PLAN_TAG}\n{secret}\n",)
    )
    assert cache_report.main([str(directory.name), "--root", str(tmp_path), "--verbose"]) == 0
    printed = capsys.readouterr().out
    assert secret not in printed
    assert "read share" in printed


def test_check_mode_exit_codes(tmp_path):
    write_session(tmp_path, "aaaaaaaa", healthy=True)
    write_session(tmp_path, "bbbbbbbb", healthy=False)
    assert cache_report.main(["aaaaaaaa", "--root", str(tmp_path), "--check"]) == 0
    assert cache_report.main(["bbbbbbbb", "--root", str(tmp_path), "--check"]) == 1
    # Without --check the report is informational, so a regression still exits 0.
    assert cache_report.main(["bbbbbbbb", "--root", str(tmp_path)]) == 0


def test_unknown_session_reports_an_error(tmp_path, capsys):
    assert cache_report.main(["abcdef01", "--root", str(tmp_path)]) == 2
    assert "Session not found" in capsys.readouterr().err


@pytest.mark.parametrize("selector", ["all", "latest"])
def test_selectors_read_real_session_layout(tmp_path, selector):
    write_session(tmp_path, "aaaaaaaa", healthy=True)
    write_session(tmp_path, "bbbbbbbb", healthy=False)
    assert cache_report.main([selector, "--root", str(tmp_path)]) == 0
