import asyncio
from dataclasses import asdict
from io import StringIO

import pytest
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from rich.console import Console

from pcode.live import AgentRuntime
from pcode.runtime import RunStatus, ToolSummary
from pcode.tool_display import label, result_detail, target
from pcode.ui import Transcript


@pytest.mark.parametrize(
    "name,args,content,expected,failed",
    [
        (
            "read_file",
            {"path": "a.py"},
            "[a.py | 5 lines | hash:abc]\n     2\tx\n"
            "     3\ty\n... (2 more lines. Use offset=3 to continue reading.)\n",
            "a.py → lines 2–3 · 2 lines · truncated",
            False,
        ),
        (
            "read_file",
            {"path": "a.py"},
            "[a.py | 0 lines | hash:abc]\n(empty file)\n",
            "a.py → 0 lines · empty file",
            False,
        ),
        (
            "search_files",
            {"path": "src", "pattern": "ToolSummary", "include_glob": "*.py"},
            "a.py:2:private text\na.py:3:private text\nb.py:1:x",
            '"ToolSummary" in src · glob "*.py" → 3 matches in 2 files',
            False,
        ),
        ("search_files", {}, "No matches found.", ". → No matches", False),
        (
            "list_directory",
            {"path": "src"},
            "src/a.py  (10 bytes)\nsrc/sub/",
            "src → 1 files · 1 directories",
            False,
        ),
        ("list_directory", {}, "(empty directory)", ". → 0 files · 0 directories", False),
        (
            "write_file",
            {"path": "a.py"},
            "Wrote 20 chars (2 lines) to a.py. [hash:abc]",
            "a.py → 2 lines written · 20 chars",
            False,
        ),
        (
            "edit_file",
            {"path": "a.py", "old_text": "a", "new_text": "b\nc"},
            "Edited a.py.",
            "a.py → +2 −1 replacement lines",
            False,
        ),
        (
            "run_command",
            {"command": "pytest -q"},
            "[stderr]\nprivate text\n[exit code: 1]",
            "pytest -q → exit 1",
            True,
        ),
        (
            "run_command",
            {"command": "pytest"},
            "[stderr]\nprivate text\n[exit code: 127]",
            "pytest → exit 127 · Executable not found",
            True,
        ),
        (
            "run_command",
            {"command": "pytest"},
            "[Command timed out after 30.0s]",
            "pytest → Timed out",
            True,
        ),
        ("run_command", {"command": "pytest"}, "(no output)", "pytest", False),
        (
            "write_plan",
            {"items": [{"status": "completed"}, {"status": "in_progress"}]},
            "ok",
            "2 steps · 1 complete · working on step 2",
            False,
        ),
        (
            "inventory_agent_context",
            {},
            {"roots": [{"exists": False}]},
            "No assistant configuration directories found",
            False,
        ),
        ("inventory_agent_context", {}, {}, "Assistant configuration inspected", False),
        ("unknown_tool", {"secret": "private text"}, "private text", "", False),
    ],
)
def test_safe_result_metrics(name, args, content, expected, failed):
    assert result_detail(name, args, content, "success") == (expected, failed)


def test_retry_is_not_reported_as_retrying_and_shows_error_body():
    detail, failed = result_detail(
        "read_file", {"path": "a.py"}, "No such file: private text", "retry"
    )
    assert failed
    assert detail == "a.py → Retry requested · No such file: private text"


def test_targets_are_relative_bounded_and_show_shell_arguments(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert target("read_file", {"path": str(tmp_path / "a.py")}) == "a.py"
    assert target("read_file", {"path": ".env"}) == "[sensitive path]"
    command = 'MODE=test python -c "print(123)" | head -n 1'
    assert target("run_command", {"command": command}) == command
    assert target("start_command", {"command": command}) == command
    assert "\x1b" not in target("read_file", {"path": "a\x1b[2J.py"})
    assert len(target("read_file", {"path": "a" * 500})) <= 90


def test_truncated_counts_are_marked():
    detail, _ = result_detail(
        "search_files", {}, "a.py:1:x\n[... truncated at 1 matches]", "success"
    )
    assert detail == ". → 1 matches in 1 files · truncated"


def test_compact_rows_and_old_and_new_event_shapes():
    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=180, color_system=None))
    old = ToolSummary("read_file", "a.py · 2 lines")
    new = ToolSummary("run_command", "pytest → exit 1", True, "call-2", 0.25)
    transcript.events((old, ToolSummary(**asdict(new))))
    assert [line.rstrip() for line in stream.getvalue().splitlines()] == [
        "✓ Read  a.py · 2 lines",
    ]


def test_tool_lines_group_together_and_are_blank_separated_from_prose():
    from pcode.runtime import Message, Thinking
    from pcode.ui import Activity

    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=80, color_system=None), activity=Activity())
    transcript.activity.show_thinking = True
    transcript.events(
        (
            Thinking("Reading first."),
            ToolSummary("read_file", "a.py · 2 lines"),
            ToolSummary("read_file", "b.py · 3 lines"),
            Message("Done."),
        )
    )
    assert [line.rstrip() for line in stream.getvalue().splitlines()] == [
        "Reading first.",
        "",
        "✓ Read  a.py · 2 lines",
        "✓ Read  b.py · 3 lines",
        "",
        "Done.",
        "",
    ]


def test_tool_summary_lines_use_the_thinking_shade():
    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=80, color_system="truecolor"))
    transcript.events((ToolSummary("read_file", "a.py · 2 lines"),))
    thinking = transcript.rich_theme.styles["pcode.thinking"]
    assert thinking.color is not None and thinking.dim
    assert thinking.render("x").split("x")[0] in stream.getvalue()


def test_parallel_calls_remain_active_until_last_result_and_keep_identity():
    async def run():
        slow_started = asyncio.Event()
        release_slow = asyncio.Event()
        requests = 0

        async def model(messages, info):
            nonlocal requests
            requests += 1
            if requests == 1:
                yield {
                    0: DeltaToolCall(
                        name="read_file", json_args='{"path":"slow.py"}', tool_call_id="slow"
                    ),
                    1: DeltaToolCall(
                        name="read_file", json_args='{"path":"fast.py"}', tool_call_id="fast"
                    ),
                }
            else:
                yield "Done"

        agent = Agent(FunctionModel(stream_function=model))

        @agent.tool_plain
        async def read_file(path: str) -> str:
            if path == "slow.py":
                slow_started.set()
                await release_slow.wait()
            else:
                # Deadlocks if the library stops executing these tools concurrently.
                await slow_started.wait()
            return f"[{path} | 1 lines | hash:abc]\n     1\tprivate text\n"

        events = []
        async for event in AgentRuntime(agent).stream("Read both"):
            events.append(event)
            if isinstance(event, RunStatus) and event.text.startswith(
                "Running read_file · slow.py"
            ):
                if any(isinstance(e, ToolSummary) and e.call_id == "fast" for e in events):
                    release_slow.set()
        summaries = [e for e in events if isinstance(e, ToolSummary)]
        assert [e.call_id for e in summaries] == ["fast", "slow"]
        assert all(e.elapsed_seconds is not None and e.elapsed_seconds >= 0 for e in summaries)
        assert summaries[0].detail.startswith("fast.py → lines 1–1")
        assert any(isinstance(e, RunStatus) and "Running 2 tools" in e.text for e in events)
        first = events.index(summaries[0])
        last = events.index(summaries[1])
        assert not any(
            isinstance(e, RunStatus) and e.text == "Waiting for model…" for e in events[first:last]
        )
        assert events[last + 1] == RunStatus("Waiting for model…")

    asyncio.run(asyncio.wait_for(run(), timeout=5))


def test_retry_feedback_and_actual_success_have_separate_summaries():
    async def run():
        requests = 0

        async def model(messages, info):
            nonlocal requests
            requests += 1
            if requests <= 2:
                yield {0: DeltaToolCall(name="retry_tool", json_args="{}")}
            else:
                yield "Done"

        agent = Agent(FunctionModel(stream_function=model))

        @agent.tool_plain
        def retry_tool() -> str:
            if requests == 1:
                raise ModelRetry("Invalid arguments: private text")
            return "private text"

        events = [e async for e in AgentRuntime(agent).stream("Try")]
        summaries = [e for e in events if isinstance(e, ToolSummary)]
        assert summaries[0].failed
        assert summaries[0].detail == "Retry requested · Invalid arguments: private text"
        assert not summaries[1].failed
        assert "private text" not in summaries[1].detail

    asyncio.run(run())


@pytest.mark.parametrize(
    "name,args,expected",
    [
        ("search_files", {"pattern": "ToolSummary", "path": "src"}, '"ToolSummary" in src'),
        ("search_files", {"pattern": ""}, '"" in .'),
        ("find_files", {"pattern": "**/*.py", "path": "tests"}, '"**/*.py" in tests'),
        ("search_files", {"pattern": "password", "path": "src"}, '"password" in src'),
    ],
)
def test_search_targets_include_patterns(name, args, expected):
    assert target(name, args) == expected


def test_command_credential_options_redacted_without_hiding_ordinary_arguments(monkeypatch):
    # Only synthetic credentials are used here; do not inspect the real environment.
    monkeypatch.setenv("EXAMPLE_API_KEY", "synthetic-display-fixture")
    command = 'client --token "dummy credential" --verbose --limit 20'
    assert target("run_command", {"command": command}) == (
        "client --token [redacted] --verbose --limit 20"
    )
    assert target("run_command", {"command": "client --api-key=synthetic-display-fixture -v"}) == (
        "client --api-key=[redacted] -v"
    )
    assert target("search_files", {"pattern": "synthetic-display-fixture"}) == '"[redacted]" in .'


def test_long_command_is_compact_without_numbers_or_details_hint():
    command = "pytest " + " ".join(f"tests/test_example_{i}.py" for i in range(20))
    assert len(target("run_command", {"command": command})) <= 100
    detail, failed = result_detail("run_command", {"command": command}, "(no output)", "success")
    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=60, color_system=None))
    transcript.command_summary(ToolSummary("run_command", detail, failed, command=command))
    output = stream.getvalue()
    assert "…" in output
    assert "tests/test_example_19.py" not in output
    assert "exit 0" not in output
    assert "/tool" not in output
    assert "#" not in output
    assert len(output.splitlines()) == 2


def test_multiline_command_preview_is_compact_and_sanitized():
    from pcode.tool_display import command_text

    command = "python - <<'PY'\n    print('hello')\nPY"
    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=80, color_system=None))
    transcript.command_summary(ToolSummary("run_command", "script → exit 0", command=command))
    assert "print('hello')" not in stream.getvalue()
    assert "1 more lines" not in stream.getvalue()
    assert "2 more lines" in stream.getvalue()
    assert "\\n" not in stream.getvalue()
    assert command_text("echo \x1b[2Jhello\u202e\n\tend") == "echo hello \n    end"


def test_command_text_redacts_before_preserving_lines():
    from pcode.tool_display import command_text

    assert command_text('client --token "dummy credential"\necho done') == (
        "client --token [redacted]\necho done"
    )


def test_command_and_query_controls_are_not_sent_to_terminal():
    assert (
        target("run_command", {"command": "echo first\necho second"})
        == "echo first … [1 more lines]"
    )
    for name, args in (
        ("run_command", {"command": "echo \x1b[2Jhello\u202e"}),
        ("search_files", {"pattern": "hello\x1b[2J\u202e"}),
    ):
        rendered = target(name, args)
        assert "\x1b" not in rendered
        assert "\u202e" not in rendered


@pytest.mark.parametrize(
    "name,args,result,expected",
    [
        ("run_command", {"command": "pytest -q tests/"}, "(no output)", "pytest -q tests/"),
        (
            "search_files",
            {"pattern": "ToolSummary", "path": "src"},
            "No matches found.",
            '"ToolSummary" in src',
        ),
    ],
)
def test_visible_inputs_reach_running_and_completed_events(name, args, result, expected):
    import json

    async def run():
        requests = 0

        async def model(messages, info):
            nonlocal requests
            requests += 1
            if requests == 1:
                yield {0: DeltaToolCall(name=name, json_args=json.dumps(args))}
            else:
                yield "Done"

        agent = Agent(FunctionModel(stream_function=model))

        @agent.tool_plain
        def run_command(command: str) -> str:
            return result

        @agent.tool_plain
        def search_files(pattern: str, path: str) -> str:
            return result

        events = [e async for e in AgentRuntime(agent).stream("Inspect")]
        assert any(isinstance(e, RunStatus) and expected in e.text for e in events)
        assert any(isinstance(e, ToolSummary) and expected in e.detail for e in events)

    asyncio.run(run())


@pytest.mark.parametrize(
    "content",
    ["(no output)", "[stdout]\nok", "[stderr]\nwarning", "[exit code: 0]", "other output"],
)
def test_successful_commands_have_no_redundant_status(content):
    assert result_detail("run_command", {"command": "echo hi"}, content, "success") == (
        "echo hi",
        False,
    )


@pytest.mark.parametrize(
    "name,args,expected",
    [
        ("get_page", {"url": "https://example.com/docs"}, "https://example.com/docs"),
        ("web_search", {"query": "python async tools"}, "python async tools"),
        ("get_page", {}, "url unavailable"),
        ("web_search", {"query": None}, "query unavailable"),
    ],
)
def test_web_tools_show_inputs_in_targets_and_results(name, args, expected):
    assert target(name, args) == expected
    assert result_detail(name, args, "private response", "success") == (expected, False)
    stream = StringIO()
    Transcript(Console(file=stream, width=180, color_system=None)).events(
        (ToolSummary(name, expected),)
    )
    assert expected in stream.getvalue()
    assert "Succeeded" not in stream.getvalue()


def test_tool_search_shows_its_queries():
    assert label("search_tools") == "Find tools"
    assert target("search_tools", {"queries": ["issues", "pull requests"]}) == (
        "issues, pull requests"
    )
    assert target("search_tools", {}) == "queries unavailable"


@pytest.mark.parametrize("outcome", ["retry", "failed"])
def test_tool_failure_shows_workspace_boundary_in_transcript(outcome):
    message = "Path '/' resolves outside the root directory."
    detail, failed = result_detail("list_directory", {"path": "/"}, message, outcome)
    assert failed
    assert message in detail
    stream = StringIO()
    Transcript(Console(file=stream, width=120, color_system=None)).events(
        (ToolSummary("list_directory", detail, failed=failed),)
    )
    assert message in stream.getvalue()
    assert "details withheld" not in stream.getvalue()


def test_tool_failure_preserves_feedback_but_redacts_credentials(monkeypatch):
    monkeypatch.setenv("TEST_API_KEY", "synthetic-environment-credential")
    message = (
        "\x1b[31mAccess rejected\x1b[0m for /tmp/repo: "
        "synthetic-environment-credential; password='two word secret'; "
        "Bearer synthetic-bearer-value\nTry a workspace-relative path."
    )
    detail, failed = result_detail("read_file", {}, message, "retry")
    assert failed
    assert "Access rejected" in detail
    assert "/tmp/repo" in detail
    assert "Try a workspace-relative path." in detail
    for secret in ("synthetic-environment-credential", "two word secret", "synthetic-bearer-value"):
        assert secret not in detail
    assert "\x1b" not in detail


def test_tool_validation_failure_shows_location_and_message_without_raw_input():
    detail, failed = result_detail(
        "read_file",
        {},
        [
            {
                "loc": ("path",),
                "msg": "Input should be a valid string",
                "input": {"private": "raw input"},
            }
        ],
        "retry",
    )
    assert failed
    assert detail == ". → Retry requested · path: Input should be a valid string"
    assert "raw input" not in detail


@pytest.mark.parametrize("content", [None, "", []])
def test_empty_tool_failure_has_explicit_fallback(content):
    assert result_detail("read_file", {}, content, "retry") == (
        ". → Retry requested · No error details returned.",
        True,
    )
