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
from pydantic_ai_harness.step_persistence import ContinuableSnapshot

from pcode.sessions import PrivateStepStore, SessionInfo

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
    store = PrivateStepStore(database=directory / "steps.sqlite3")
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


def test_delegated_runs_are_scored_separately(tmp_path):
    """A sub-agent's history is a different conversation sharing the same store."""
    directory = write_session(tmp_path, "fffffff6", healthy=True)
    store = PrivateStepStore(database=directory / "steps.sqlite3")
    history: list = [ModelRequest(parts=[UserPromptPart(content="task")])]

    async def build() -> None:
        for index in range(4):
            # A child that reuses nothing: every request re-reads its prefix.
            history.append(response(100 + index, read=0, write=2000, total=4000))
            history.append(ModelRequest(parts=[UserPromptPart(content="more")]))
            await store.save_snapshot(
                ContinuableSnapshot(
                    run_id="child",
                    step_index=index,
                    messages=list(history),
                    parent_run_id="run",
                )
            )

    asyncio.run(build())
    report = cache_report.analyze(directory)
    # The parent's own score is unaffected by the child's traffic.
    assert [f.level for f in report.findings if not f.message.startswith("delegated")] == ["ok"]
    assert len(report.delegated) == 4
    delegated = [f for f in report.findings if f.message.startswith("delegated")]
    assert delegated and delegated[0].level == "fail"
    assert "delegated: requests=4" in cache_report.render(report)


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


def count_rewrites(tmp_path: Path, *snapshots: list) -> int:
    database = tmp_path / "steps.sqlite3"
    store = PrivateStepStore(database=database)

    async def build() -> None:
        for index, messages in enumerate(snapshots):
            await store.save_snapshot(
                ContinuableSnapshot(run_id="run", step_index=index, messages=messages)
            )

    asyncio.run(build())
    return cache_report._count_prefix_rewrites(database, delegated=False)


def tool_return(index: int, content: str = "r") -> ToolReturnPart:
    return ToolReturnPart(tool_name="probe", content=content, tool_call_id=f"call_{index}")


def test_merged_requests_are_not_a_rewrite(tmp_path):
    """Pydantic AI merges a loaded history's consecutive requests, tool results first.

    The provider sees the same blocks either way, so this is not a replaced prefix.
    """
    start = ModelRequest(parts=[UserPromptPart(content="start")])
    reminder = UserPromptPart(content=f"{cache_report.PLAN_TAG}\nA\n")
    stored = [
        start,
        response(0, read=0, write=0, total=0),
        ModelRequest(parts=[reminder]),
        ModelRequest(parts=[tool_return(0)]),
        response(1, read=0, write=0, total=0),
        ModelRequest(parts=[tool_return(1)]),
    ]
    loaded = [
        start,
        response(0, read=0, write=0, total=0),
        ModelRequest(parts=[tool_return(0), reminder]),
        *stored[4:],
        response(2, read=0, write=0, total=0),
    ]
    assert count_rewrites(tmp_path, stored, loaded) == 0


def test_note_after_a_settled_tool_result_extends_the_prefix(tmp_path):
    """A discarded in-flight response replaced by a note leaves every sent block in place."""
    head = [
        ModelRequest(parts=[UserPromptPart(content="start")]),
        response(0, read=0, write=0, total=0),
        ModelRequest(parts=[tool_return(0)]),
    ]
    interrupted = [*head, response(1, read=0, write=0, total=0), ModelRequest(parts=[])]
    resumed = [
        *head,
        ModelRequest(parts=[UserPromptPart(content="note")]),
        response(2, read=0, write=0, total=0),
    ]
    assert count_rewrites(tmp_path, interrupted, resumed) == 0


def test_changed_settled_content_is_a_rewrite(tmp_path):
    history = [
        ModelRequest(parts=[UserPromptPart(content="start")]),
        response(0, read=0, write=0, total=0),
        ModelRequest(parts=[tool_return(0)]),
        response(1, read=0, write=0, total=0),
        ModelRequest(parts=[tool_return(1)]),
    ]
    rewritten = [*history[:2], ModelRequest(parts=[tool_return(0, "changed")]), *history[3:]]
    assert count_rewrites(tmp_path, history, rewritten) == 1


def test_short_session_is_not_reported_as_a_regression(tmp_path):
    """Below the provider's minimum cacheable size, zero reads is expected."""
    directory = tmp_path / "eeeeeee5"
    directory.mkdir()
    write_info(directory, "eeeeeee5", model="test")
    store = PrivateStepStore(database=directory / "steps.sqlite3")
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


def _write_antigravity_session(
    root: Path, identity: str, responses: list[ModelResponse]
) -> Path:
    directory = root / identity
    directory.mkdir(parents=True)
    write_info(directory, identity, model="antigravity:gemini-4-neon")
    store = PrivateStepStore(database=directory / "steps.sqlite3")
    history: list = []

    async def build() -> None:
        for index, resp in enumerate(responses):
            history.append(ModelRequest(parts=[UserPromptPart(content=f"turn {index}")]))
            history.append(resp)
            await store.save_snapshot(
                ContinuableSnapshot(run_id="run", step_index=index, messages=list(history))
            )

    asyncio.run(build())
    return directory


def _ag_response(
    index: int,
    *,
    session_id: str = "agy-sess-1",
    turn: int | None = None,
    read: int,
    total: int,
    output: int = 500,
    context_used: int | None = None,
) -> ModelResponse:
    details = {"stop_reason": "end_turn"}
    if context_used is not None:
        details["context_used"] = context_used
    return ModelResponse(
        parts=[TextPart(f"reply {index}")],
        usage=RequestUsage(
            input_tokens=total,
            output_tokens=output,
            cache_read_tokens=read,
        ),
        model_name="gemini-4-neon",
        provider_name="antigravity",
        provider_response_id=f"{session_id}#{turn or index + 1}",
        provider_details=details,
        timestamp=datetime(2026, 1, 1, 0, index // 60, index % 60, tzinfo=timezone.utc),
    )


def test_antigravity_short_followup_after_long_turn_is_healthy(tmp_path):
    """A 1-call follow-up after a 50-call turn must not fail against the prior turn's sum."""
    directory = _write_antigravity_session(
        tmp_path,
        "ag000001",
        [
            _ag_response(0, read=4_200_000, total=4_400_000, context_used=180_000),
            _ag_response(1, read=182_000, total=185_000, context_used=185_000),
            _ag_response(2, read=370_000, total=380_000, context_used=192_000),
        ],
    )
    report = cache_report.analyze(directory)
    assert [f.level for f in report.findings] == ["ok"]
    assert "3/3 eligible turns" in report.findings[0].message


def test_antigravity_unbilled_continuations_are_excluded(tmp_path):
    """Both post-fix (input=0) and legacy (input==context_used, read=0, out=0) continuations."""
    directory = _write_antigravity_session(
        tmp_path,
        "ag000002",
        [
            _ag_response(0, read=150_000, total=160_000, context_used=60_000),
            # Legacy pre-fix continuation fallback: input == context_used, read=0, out=0.
            _ag_response(1, read=0, total=65_000, output=0, context_used=65_000),
            # Post-fix continuation: input=0, read=0, out=0, context_used set.
            _ag_response(2, read=0, total=0, output=0, context_used=70_000),
            _ag_response(3, read=142_000, total=145_000, context_used=74_000),
        ],
    )
    report = cache_report.analyze(directory)
    assert len(report.requests) == 2
    assert report.unbilled_turns == 2
    assert [f.level for f in report.findings] == ["ok"]
    assert report.read_share > 0.95
    assert "unbilled continuations=2" in cache_report.render(report)


def test_antigravity_real_cache_miss_still_fails(tmp_path):
    """A real follow-up turn with output>0 and read=0 is still flagged as a miss."""
    directory = _write_antigravity_session(
        tmp_path,
        "ag000003",
        [
            _ag_response(0, read=0, total=150_000, output=800, context_used=150_000),
            _ag_response(1, read=0, total=155_000, output=600, context_used=155_000),
        ],
    )
    report = cache_report.analyze(directory)
    assert [f.level for f in report.findings] == ["fail"]
    assert "0/1 eligible turns" in report.findings[0].message


def test_antigravity_cold_short_multicall_turn_is_healthy(tmp_path):
    """A cold 2-call turn with 100% reuse on call 2 has <50% raw share but 100% cacheable reuse."""
    directory = _write_antigravity_session(
        tmp_path,
        "ag000005",
        [
            # Call 1 cold (13,165), call 2 hits 13,165 of 17,760 -> total 30,925, ctx 17,760.
            _ag_response(0, read=13_165, total=30_925, context_used=17_760),
        ],
    )
    report = cache_report.analyze(directory)
    assert [f.level for f in report.findings] == ["ok"]
    assert "1/1 eligible turns" in report.findings[0].message


def test_antigravity_interleaved_side_session_does_not_taint_main_session(tmp_path):
    """A single-call /btw side session starts cold and is not judged against main context."""
    directory = _write_antigravity_session(
        tmp_path,
        "ag000004",
        [
            _ag_response(
                0, session_id="main", turn=1, read=300_000, total=350_000, context_used=80_000
            ),
            _ag_response(1, session_id="btw", turn=1, read=0, total=40_000, context_used=40_000),
            _ag_response(
                2, session_id="main", turn=2, read=162_000, total=166_000, context_used=84_000
            ),
        ],
    )
    report = cache_report.analyze(directory)
    assert [f.level for f in report.findings] == ["ok"]
    assert "2/2 eligible turns" in report.findings[0].message


def test_mixed_provider_session_scores_both_groups(tmp_path):
    """Switching between Anthropic and Antigravity still catches Antigravity misses."""
    directory = _write_antigravity_session(
        tmp_path,
        "ag000006",
        [
            response(0, read=0, write=4000, total=4000),
            response(1, read=4000, write=300, total=4300),
            _ag_response(2, read=0, total=150_000, output=800, context_used=150_000),
            _ag_response(3, read=0, total=155_000, output=600, context_used=155_000),
        ],
    )
    report = cache_report.analyze(directory)
    levels = [f.level for f in report.findings]
    assert "ok" in levels
    assert "fail" in levels
    assert any("0/1 eligible turns" in f.message for f in report.findings)


def test_antigravity_compaction_shrinks_prefix_baseline(tmp_path):
    """After compaction drops context_used, the next turn is judged against the compacted size."""
    directory = _write_antigravity_session(
        tmp_path,
        "ag000007",
        [
            _ag_response(0, read=1_000_000, total=1_400_000, context_used=400_000),
            # Compaction happened in-place on the same session_id, leaving 50k context.
            _ag_response(1, read=45_000, total=50_000, context_used=50_000),
            # 1-call follow-up with 25k new prompt hitting 100% of the 50k compacted prefix.
            _ag_response(2, read=50_000, total=75_000, context_used=75_000),
        ],
    )
    report = cache_report.analyze(directory)
    assert [f.level for f in report.findings] == ["ok"]
    assert "3/3 eligible turns" in report.findings[0].message


def test_antigravity_legacy_to_modern_context_used_transition(tmp_path):
    """A legacy turn without context_used must not pin the baseline above later context_used."""
    directory = _write_antigravity_session(
        tmp_path,
        "ag000008",
        [
            _ag_response(0, read=4_000_000, total=4_400_000, context_used=None),
            _ag_response(1, read=170_000, total=180_000, context_used=180_000),
            # Follow-up with 60k new prompt hitting 100% of the 180k context.
            _ag_response(2, read=180_000, total=240_000, context_used=240_000),
        ],
    )
    report = cache_report.analyze(directory)
    assert [f.level for f in report.findings] == ["ok"]
    assert "2/2 eligible turns" in report.findings[0].message


def test_antigravity_unbilled_continuation_advances_prefix_baseline(tmp_path):
    """Unbilled continuations still update context_used so a subsequent miss is caught."""
    directory = _write_antigravity_session(
        tmp_path,
        "ag000009",
        [
            _ag_response(0, read=100_000, total=160_000, context_used=60_000),
            # Unbilled continuation grows context from 60k to 150k.
            _ag_response(1, read=0, total=0, output=0, context_used=150_000),
            # Partial cache drop: only 50k of the 150k context reused -> should miss.
            _ag_response(2, read=50_000, total=155_000, output=500, context_used=155_000),
        ],
    )
    report = cache_report.analyze(directory)
    assert report.unbilled_turns == 1
    assert [f.level for f in report.findings] == ["warn"]
    assert "1/2 eligible turns" in report.findings[0].message


def test_antigravity_delegated_workers_scored_by_session_id(tmp_path):
    """Delegated Antigravity workers with separate session IDs are scored independently."""
    directory = _write_antigravity_session(
        tmp_path,
        "ag000010",
        [_ag_response(0, session_id="parent", read=300_000, total=350_000, context_used=80_000)],
    )
    store = PrivateStepStore(database=directory / "steps.sqlite3")
    worker_msgs = [
        ModelRequest(parts=[UserPromptPart(content="w1")]),
        _ag_response(
            1, session_id="worker-1", turn=1, read=180_000, total=220_000, context_used=40_000
        ),
        ModelRequest(parts=[UserPromptPart(content="w2")]),
        _ag_response(2, session_id="worker-2", turn=1, read=0, total=40_000, context_used=40_000),
    ]
    asyncio.run(
        store.save_snapshot(
            ContinuableSnapshot(
                run_id="child", step_index=0, messages=worker_msgs, parent_run_id="run"
            )
        )
    )
    report = cache_report.analyze(directory)
    delegated = [f for f in report.findings if f.message.startswith("delegated:")]
    assert len(delegated) == 1
    assert delegated[0].level == "ok"
    assert "1/1 eligible turns" in delegated[0].message
