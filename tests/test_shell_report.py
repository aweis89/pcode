"""The shell report must read what the model saw and flag the known misuse patterns."""

import asyncio
import importlib.util
import sys
from pathlib import Path

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, SqliteStepStore

from pcode.sessions import SessionInfo
from pcode.shell import REDUCED_SHELL_OUTPUT

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "shell_report.py"
_spec = importlib.util.spec_from_file_location("shell_report", SCRIPT)
shell_report = importlib.util.module_from_spec(_spec)
# `@dataclass` resolves its module through `sys.modules`, so a script loaded by
# path must be registered there before it is executed.
sys.modules["shell_report"] = shell_report
_spec.loader.exec_module(shell_report)

WORKSPACE = "/work/repo"


def write_session(root: Path, identity: str, calls: list[tuple[str, dict, str]]) -> Path:
    """Persist one call/return pair per step through the real store.

    Every snapshot carries the cumulative history, which is exactly the
    duplication the report has to see through.
    """
    directory = root / identity
    directory.mkdir(parents=True)
    info = SessionInfo(
        id=identity,
        model="anthropic:claude-opus-5",
        workspace=WORKSPACE,
        created="2026-01-01T00:00:00+00:00",
        updated="2026-01-01T00:00:00+00:00",
        packages={},
    )
    (directory / "session.json").write_text(info.model_dump_json())
    store = SqliteStepStore(database=directory / "steps.sqlite3")
    history: list = [ModelRequest(parts=[UserPromptPart(content="start")])]

    async def build() -> None:
        for index, (tool, args, result) in enumerate(calls):
            call_id = f"call_{index}"
            history.append(
                ModelResponse(
                    parts=[ToolCallPart(tool_name=tool, args=args, tool_call_id=call_id)],
                    model_name="claude-opus-5",
                )
            )
            history.append(
                ModelRequest(
                    parts=[ToolReturnPart(tool_name=tool, content=result, tool_call_id=call_id)]
                )
            )
            await store.save_snapshot(
                ContinuableSnapshot(run_id="run", step_index=index, messages=list(history))
            )
        history.append(
            ModelRequest(parts=[UserPromptPart(content="[j2] make test → exit 1 after 30.0s.")])
        )
        history.append(ModelResponse(parts=[TextPart("done")], model_name="claude-opus-5"))
        await store.save_snapshot(
            ContinuableSnapshot(run_id="run", step_index=len(calls), messages=list(history))
        )

    asyncio.run(build())
    return directory


def shell(command: str, result: str, **args) -> tuple[str, dict, str]:
    return "shell", {"command": command, **args}, result


def test_counts_each_call_once_and_parses_the_job_marker(tmp_path):
    directory = write_session(
        tmp_path,
        "aaaaaaa1",
        [
            shell("git status --short", "M a\n[j1 · exit 0 · 120ms]"),
            shell(
                "make test",
                "[j2 · running · pid 7 · 2ms] Started in the background.",
                background=True,
                purpose="running tests",
            ),
            ("wait_for_job", {"job_id": "j2"}, "FAILED tests/x.py\n[j2 · exit 1 · 4m21s]"),
        ],
    )

    report = shell_report.analyze(directory)

    assert [c.tool for c in report.calls] == ["shell", "shell", "wait_for_job"]
    first, background, wait = report.calls
    assert (first.job, first.exit_code, first.elapsed) == ("j1", 0, 0.12)
    assert background.running and background.background and background.purpose == "running tests"
    assert (wait.job, wait.exit_code, wait.elapsed) == ("j2", 1, 261.0)
    assert report.notices == 1
    assert [f.level for f in report.findings] == ["ok"]


def test_flags_polling_masked_failures_and_cd_prefixes(tmp_path):
    directory = write_session(
        tmp_path,
        "bbbbbbb2",
        [
            shell(
                f"cd {WORKSPACE} && make test 2>&1 | tail -3",
                "1 failed\nmake: *** [test] Error 1\n[j1 · exit 0 · 30.0s]",
            ),
            shell(f"cd {WORKSPACE} && sleep 30; cat /tmp/status.json", "{}\n[j2 · exit 0 · 30.0s]"),
            shell("pkill -f old; sleep 2; make run", "ok\n[j3 · exit 0 · 3.0s]"),
            shell("for i in 1 2 3; do sleep 1; done", "\n[j4 · exit 0 · 3.0s]"),
            shell(
                f"cd {WORKSPACE}/ && sed -n 1,20p README.md",
                f"{REDUCED_SHELL_OUTPUT}\n...\n[j5 · exit 0 · 90ms]",
            ),
        ],
    )

    report = shell_report.analyze(directory)
    messages = {f.level: [] for f in report.findings}
    for finding in report.findings:
        messages[finding.level].append(finding.message)

    assert any(m.startswith("1 command(s) sleep") for m in messages["warn"]), report.findings
    assert any(m.startswith("1 result(s) exit 0 with failure text") for m in messages["warn"])
    assert any(m.startswith("3/5 commands start with cd") for m in messages["info"])
    assert any(m.startswith("1 read-only command") for m in messages["info"])
    assert any(m.startswith("1 result(s) were reduced") for m in messages["info"])


def test_verbose_render_redacts_commands(tmp_path):
    directory = write_session(
        tmp_path,
        "ccccccc3",
        [shell("curl -H 'Authorization: Bearer abc123' https://x", "[j1 · exit 0 · 50ms]")],
    )

    text = shell_report.render(shell_report.analyze(directory), verbose=True)

    assert "abc123" not in text
    assert "j1  shell" in text


def test_main_reports_a_missing_step_store(tmp_path, capsys):
    directory = tmp_path / "ddddddd4"
    directory.mkdir()
    info = SessionInfo(
        id="ddddddd4",
        model="m",
        workspace=WORKSPACE,
        created="2026-01-01T00:00:00+00:00",
        updated="2026-01-01T00:00:00+00:00",
        packages={},
    )
    (directory / "session.json").write_text(info.model_dump_json())

    assert shell_report.main(["ddddddd4", "--root", str(tmp_path)]) == 0
    assert "No step store" in capsys.readouterr().err
