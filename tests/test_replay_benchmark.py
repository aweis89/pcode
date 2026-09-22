import asyncio
import hashlib
import json
import sys
from contextlib import closing
from io import StringIO
from unittest.mock import patch

import pytest

from pcode.profile_benchmark import benchmark_output, main
from pcode.replay_benchmark import (
    MAX_GAP_SECONDS,
    JournalRuntime,
    JournalSnapshot,
    ReplaySettings,
    journal_turns,
    replay_journal,
)
from pcode.ui import TerminalOutput


def journal(tmp_path, records):
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join(json.dumps({"version": 1, **r}) + "\n" for r in records))
    return path


def replay(path, *, mode="streamed", settings=None):
    output, _ = benchmark_output(80, mode=mode)
    sink = StringIO()
    output.console.file = sink
    with closing(JournalSnapshot(path)) as snapshot:
        result = asyncio.run(replay_journal(snapshot, output, settings or ReplaySettings()))
    assert output.tail == output._thinking_tail == ""
    return result, sink.getvalue()


@pytest.mark.parametrize("mode", ["streamed", "end-only"])
def test_real_text_and_thinking_paths_do_not_duplicate_completions(tmp_path, mode):
    path = journal(
        tmp_path,
        [
            {"kind": "turn_started", "prompt": "USER_SENTINEL"},
            {"kind": "ThinkingDelta", "text": "THINKING_SENTINEL\n\n"},
            {"kind": "Thinking", "text": "THINKING_SENTINEL\n\n"},
            {"kind": "TextDelta", "text": "ANSWER_SENTINEL\n\n"},
            {"kind": "Message", "markdown": "ANSWER_SENTINEL\n\n"},
            {"kind": "turn_completed"},
        ],
    )
    original = path.read_bytes()
    result, output = replay(path, mode=mode)
    assert output.count("USER_SENTINEL") == 1
    assert output.count("THINKING_SENTINEL") == 1
    assert output.count("ANSWER_SENTINEL") == 1
    assert result["turns"] == 1
    assert result["event_counts"]["TextDelta"] == 1
    assert result["render_cpu_seconds"] > 0
    assert result["cpu_seconds"] >= result["render_cpu_seconds"]
    assert result["journal_sha256"] == hashlib.sha256(original).hexdigest()
    assert path.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["transcript.jsonl"]
    hidden, output = replay(path, mode=mode, settings=ReplaySettings(show_thinking=False))
    assert "THINKING_SENTINEL" not in output
    assert "ANSWER_SENTINEL" in output
    assert hidden["event_counts"] == result["event_counts"]


@pytest.mark.parametrize("boundary", ["turn_failed", "turn_cancelled", "turn_completed"])
def test_partial_output_attempt_boundaries_and_fallbacks(tmp_path, boundary):
    path = journal(
        tmp_path,
        [
            {"kind": "turn_started", "prompt": "first", "run_id": "one"},
            {"kind": "TextDelta", "text": "PARTIAL_FIRST"},
            {"kind": boundary},
            {"kind": "turn_started", "prompt": "second", "run_id": "two", "continuation": True},
            {"kind": "Thinking", "text": "THINK_FALLBACK"},
            {"kind": "Message", "markdown": "ANSWER_FALLBACK"},
            {"kind": "TextDelta", "text": "UNFINISHED_EOF"},
        ],
    )
    result, output = replay(path)
    assert result["turns"] == 2
    for text in ("PARTIAL_FIRST", "THINK_FALLBACK", "ANSWER_FALLBACK", "UNFINISHED_EOF"):
        assert output.count(text) == 1
    assert output.index("PARTIAL_FIRST") < output.index("ANSWER_FALLBACK")


def test_empty_opposite_delta_and_hidden_tool_boundaries(tmp_path):
    records = [
        {"kind": "TextDelta", "text": "BEFORE"},
        {"kind": "ToolStarted", "name": "write_plan", "detail": "", "call_id": "c"},
        {"kind": "ToolSummary", "name": "write_plan", "detail": "", "call_id": "c"},
        {"kind": "TextDelta", "text": "AFTER"},
        {"kind": "ThinkingDelta", "text": ""},
        {"kind": "TextDelta", "text": "SEPARATE"},
    ]
    _, output = replay(journal(tmp_path, records))
    assert "BEFOREAFTER" in output
    assert "AFTERSEPARATE" not in output


def test_tool_edit_cache_and_plan_events_use_real_presentation_only(tmp_path, monkeypatch):
    from pcode import ui

    def forbidden(*args, **kwargs):
        pytest.fail("unexpected runtime/config/terminal I/O")

    monkeypatch.setattr(ui, "load_preferences", forbidden)
    monkeypatch.setattr(ui, "detect_theme", forbidden)
    path = journal(
        tmp_path,
        [
            {"kind": "turn_started", "prompt": "a"},
            {"kind": "PlanUpdated", "items": []},
            {"kind": "RunStatus", "text": "status"},
            {
                "kind": "ToolSummary",
                "name": "shell",
                "detail": "",
                "command": "NEVER_EXECUTE",
                "result": "COMMAND_OUTPUT",
                "call_id": "c",
            },
            {"kind": "ToolSummary", "name": "read_file", "detail": "FAILED_FILE", "failed": True},
            {
                "kind": "EditCompleted",
                "call_id": "e",
                "path": "fictional.py",
                "operation": "edit",
                "patch": "@@ -1 +1 @@\n-old\n+new\n",
                "added": 1,
                "removed": 1,
            },
            {"kind": "CacheBust", "text": "CACHE_WARNING"},
        ],
    )
    with (
        patch("subprocess.Popen", side_effect=forbidden),
        patch("socket.create_connection", forbidden),
    ):
        result, output = replay(path, settings=ReplaySettings(command_scrollback=True))
    assert "COMMAND_OUTPUT" in output
    assert "FAILED_FILE" in output
    assert "fictional.py" in output
    assert "CACHE_WARNING" in output
    assert result["event_counts"]["ToolSummary"] == 2


def test_all_branches_in_append_order_and_turn_selection(tmp_path):
    records = []
    for n in range(1, 4):
        records += [
            {"kind": "turn_started", "run_id": str(n), "parent_id": None, "prompt": "prompt"},
            {"kind": "Message", "markdown": f"RESPONSE_{n}"},
            {"kind": "turn_completed"},
            {"kind": "tree_selected", "node_id": None},
        ]
    records.append({"kind": "compaction_checkpoint", "messages": "must not deserialize"})
    path = journal(tmp_path, records)
    result, output = replay(path)
    assert all(f"RESPONSE_{n}" in output for n in range(1, 4))
    assert result["skipped_records"]["metadata"] == 4
    result, output = replay(path, settings=ReplaySettings(start_turn=2, max_turns=1))
    assert "RESPONSE_2" in output
    assert "RESPONSE_1" not in output and "RESPONSE_3" not in output
    assert result["turns"] == 1
    assert result["top_turns"][0]["turn"] == 2


def test_end_only_experiment_skips_incremental_parser_not_final_render(tmp_path):
    path = journal(
        tmp_path,
        [
            *({"kind": "TextDelta", "text": f"- item {i}\n"} for i in range(10)),
            {"kind": "Message", "markdown": "unused fallback"},
        ],
    )
    with patch.object(TerminalOutput, "_commit_blocks", autospec=True) as parse:
        result, output = replay(path, mode="end-only")
    parse.assert_not_called()
    assert "item 0" in output and "item 9" in output
    assert "unused fallback" not in output
    assert result["event_counts"]["TextDelta"] == 10


def test_bad_unknown_unsupported_records_counted_without_content_leaks(tmp_path):
    path = journal(
        tmp_path,
        [
            {"kind": "PRIVATE_KIND"},
            {"kind": "Message", "markdown": "PRIVATE_INVALID", "version": 999},
            {"kind": "Message", "markdown": {"PRIVATE_BAD_TYPE": "text"}},
            {"kind": ["PRIVATE_LIST"]},
            {"kind": "Message", "markdown": "okay"},
        ],
    )
    with path.open("ab") as file:
        file.write(b'[]\n{"PRIVATE_TORN":')
    result, output = replay(path)
    assert "okay" in output
    assert "PRIVATE" not in output + json.dumps(result)
    assert result["skipped_records"] == {
        "malformed": 3,
        "unknown_kind": 1,
        "unsupported_version": 1,
        "invalid_event": 1,
    }


def test_snapshot_ignores_appends_and_detects_overwrites(tmp_path):
    path = journal(tmp_path, [{"kind": "Message", "markdown": "one"}])
    with closing(JournalSnapshot(path)) as snapshot:
        first = list(snapshot.records())
        with path.open("ab") as file:
            file.write(b'{"kind":"Message","markdown":"extra"}\n')
        assert list(snapshot.records()) == first
        path.write_bytes(path.read_bytes().replace(b"one", b"two"))
        with pytest.raises(ValueError, match="changed"):
            list(snapshot.records())


def test_snapshot_bounds_oversized_lines_and_rejects_symlinks(tmp_path, monkeypatch):
    from pcode import replay_benchmark

    monkeypatch.setattr(replay_benchmark, "MAX_RECORD_BYTES", 100)
    path = journal(
        tmp_path,
        [
            {"kind": "Message", "markdown": "x" * 300},
            {"kind": "Message", "markdown": "okay"},
        ],
    )
    result, output = replay(path)
    assert "okay" in output
    assert result["skipped_records"]["oversized"] == 1
    link = tmp_path / "link.jsonl"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="symlink"):
        JournalSnapshot(link)


def test_cli_comparison_outputs_only_metrics_and_never_opens_sessions(
    tmp_path, monkeypatch, capsys
):
    path = journal(
        tmp_path,
        [
            {"kind": "Message", "markdown": "PRIVATE_MESSAGE_SENTINEL"},
        ],
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pcode-benchmark",
            "--journal",
            str(path),
            "--render-mode",
            "both",
            "--repeat",
            "2",
        ],
    )
    with (
        patch("pcode.sessions.SavedSession.open", side_effect=AssertionError),
        patch("pcode.sessions.SavedSession.create", side_effect=AssertionError),
    ):
        main()
    captured = capsys.readouterr()
    assert "PRIVATE_MESSAGE_SENTINEL" not in captured.out + captured.err
    results = [json.loads(line) for line in captured.out.splitlines()]
    assert [r["render_mode"] for r in results] == ["streamed", "end-only", "end-only", "streamed"]
    assert len({r["journal_sha256"] for r in results}) == 1


def test_cli_failure_is_sanitized_and_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv", ["pcode-benchmark", "--journal", str(tmp_path / "PRIVATE_NAME")]
    )
    with pytest.raises(SystemExit) as raised:
        main()
    captured = capsys.readouterr()
    assert raised.value.code == 1
    assert "PRIVATE_NAME" not in captured.out + captured.err
    assert json.loads(captured.out)["error"] == "FileNotFoundError"


def test_cli_selects_recent_sessions_readonly_and_profiles_separate_passes(
    tmp_path, monkeypatch, capsys
):
    from pcode.sessions import SessionInfo

    root = tmp_path / "sessions"
    paths = []
    for number in (1, 2):
        identity = f"00000000-0000-0000-0000-{number:012d}"
        directory = root / identity
        directory.mkdir(parents=True)
        info = SessionInfo(
            id=identity,
            model="test:offline",
            workspace="PRIVATE_WORKSPACE",
            created=f"2025-01-0{number}",
            updated=f"2025-01-0{number}",
            packages={},
        )
        (directory / "session.json").write_text(info.model_dump_json())
        journal(directory, [{"kind": "Message", "markdown": "PRIVATE_TEXT" * number}])
        paths.append(directory / "transcript.jsonl")
    original = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    destination = tmp_path / "profiles"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pcode-benchmark",
            "--session-dir",
            str(root),
            "--recent",
            "2",
            "--profile",
            str(destination),
            "--profile-cpu",
        ],
    )
    with patch("pcode.sessions.SavedSession.open", side_effect=AssertionError):
        main()
    captured = capsys.readouterr()
    results = [json.loads(line) for line in captured.out.splitlines()]
    assert len(results) == 2
    assert results[0]["journal_sha256"] == hashlib.sha256(paths[1].read_bytes()).hexdigest()
    assert results[1]["journal_sha256"] == hashlib.sha256(paths[0].read_bytes()).hexdigest()
    assert "PRIVATE" not in captured.out + captured.err
    assert all((destination / f"session-{n}-streamed-1" / "cpu.pstats").exists() for n in (1, 2))
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == original


def test_cli_selects_largest_journals_by_size(tmp_path, monkeypatch, capsys):
    from pcode.sessions import SessionInfo

    root = tmp_path / "sessions"
    digests = {}
    # Newest is the smallest, so recency order and size order disagree.
    for number, lines in ((1, 30), (2, 10), (3, 1)):
        identity = f"00000000-0000-0000-0000-{number:012d}"
        directory = root / identity
        directory.mkdir(parents=True)
        info = SessionInfo(
            id=identity,
            model="test:offline",
            workspace="PRIVATE_WORKSPACE",
            created=f"2025-01-0{number}",
            updated=f"2025-01-0{number}",
            packages={},
        )
        (directory / "session.json").write_text(info.model_dump_json())
        path = journal(directory, [{"kind": "Message", "markdown": "PRIVATE_TEXT"}] * lines)
        digests[number] = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(
        sys, "argv", ["pcode-benchmark", "--session-dir", str(root), "--largest", "2"]
    )
    main()
    captured = capsys.readouterr()
    results = [json.loads(line) for line in captured.out.splitlines()]
    assert [r["journal_sha256"] for r in results] == [digests[1], digests[2]]
    assert "PRIVATE" not in captured.out + captured.err


def test_journal_turns_pace_events_by_timestamp_with_capped_gaps(tmp_path):
    path = journal(
        tmp_path,
        [
            {"kind": "TextDelta", "text": "legacy, before any turn marker"},
            {"kind": "turn_started", "prompt": "first", "time": "2025-01-01T00:00:00+00:00"},
            {"kind": "TextDelta", "text": "a", "time": "2025-01-01T00:00:00.250000+00:00"},
            {"kind": "TextDelta", "text": "b", "time": "2025-01-01T00:05:00+00:00"},
            {"kind": "TextDelta", "text": "c", "time": "not a time"},
            {"kind": "TextDelta", "text": "d", "time": "2025-01-01T00:04:00+00:00"},
            {"kind": "turn_completed"},
            {"kind": "turn_started", "prompt": "second"},
            {"kind": "tree_selected"},
            {"kind": "TextDelta", "text": 5},
            {"kind": "turn_started", "prompt": "third"},
            {"kind": "Message", "markdown": "done"},
        ],
    )
    with closing(JournalSnapshot(path)) as snapshot:
        turns = journal_turns(snapshot)
    assert [turn.prompt for turn in turns] == ["", "first", "third"]
    first = turns[1]
    assert [text for _, event in first.events for text in [event.text]] == list("abcd")
    gaps = [gap for gap, _ in first.events]
    # A tool that ran for minutes is capped; unparsable and backwards stamps wait nothing.
    assert gaps == [0.25, MAX_GAP_SECONDS, 0.0, 0.0]


def test_journal_runtime_plays_one_turn_per_stream_and_scales_pacing(tmp_path, monkeypatch):
    path = journal(
        tmp_path,
        [
            {"kind": "turn_started", "prompt": "one", "time": "2025-01-01T00:00:00+00:00"},
            {"kind": "TextDelta", "text": "a", "time": "2025-01-01T00:00:01+00:00"},
            {"kind": "turn_started", "prompt": "two"},
            {"kind": "TextDelta", "text": "b"},
        ],
    )
    with closing(JournalSnapshot(path)) as snapshot:
        runtime = JournalRuntime(journal_turns(snapshot), speed=4)
    slept = []

    async def fake_sleep(delay):
        slept.append(delay)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    async def drain():
        return [event.text async for event in runtime.stream("ignored")]

    assert asyncio.run(drain()) == ["a"]
    assert asyncio.run(drain()) == ["b"]
    assert slept == [0.25, 0]
    assert runtime.events_played == 2


def test_live_mode_refuses_without_a_terminal(tmp_path, monkeypatch, capsys):
    path = journal(tmp_path, [{"kind": "Message", "markdown": "PRIVATE_TEXT"}])
    monkeypatch.setattr(sys, "argv", ["pcode-benchmark", "--live", "--journal", str(path)])
    monkeypatch.setattr("sys.stdin", StringIO())
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert "needs a terminal" in captured.err
    assert "PRIVATE" not in captured.out + captured.err


def test_cli_replay_prefix_and_empty_store_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv", ["pcode-benchmark", "--session-dir", str(tmp_path), "--recent", "2"]
    )
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2
    assert "no saved sessions" in capsys.readouterr().err
    monkeypatch.setattr(sys, "argv", ["pcode-benchmark", "--render-mode", "both"])
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2


def test_turn_range_settles_only_the_selected_attempt_once(tmp_path):
    records = []
    for index in range(5):
        records.extend(
            [
                {"kind": "turn_started", "prompt": str(index)},
                {"kind": "TextDelta", "text": "partial"},
                {"kind": "turn_completed"},
            ]
        )
    path = journal(tmp_path, records)
    original = TerminalOutput.end_turn
    with patch.object(TerminalOutput, "end_turn", autospec=True, side_effect=original) as end:
        result, _ = replay(path, settings=ReplaySettings(start_turn=2, max_turns=1))
    assert end.call_count == 1
    assert result["render_cpu_seconds"] == result["top_turns"][0]["render_cpu_seconds"]


def test_repeated_passes_release_retained_transcripts_outside_measurement(
    tmp_path, monkeypatch, capsys
):
    import weakref

    from pcode import profile_benchmark

    path = journal(tmp_path, [{"kind": "Message", "markdown": "temporary transcript"}])
    references = []
    original = profile_benchmark.benchmark_output

    def create_output(*args, **kwargs):
        assert all(ref() is None for ref in references)
        output, sink = original(*args, **kwargs)
        references.append(weakref.ref(output))
        return output, sink

    monkeypatch.setattr(profile_benchmark, "benchmark_output", create_output)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pcode-benchmark",
            "--journal",
            str(path),
            "--repeat",
            "2",
            "--render-mode",
            "both",
        ],
    )
    main()
    assert len(references) == 4
    assert all(ref() is None for ref in references)
    assert len(capsys.readouterr().out.splitlines()) == 4


def test_symlinked_session_directory_is_rejected(tmp_path):
    directory = tmp_path / "real"
    directory.mkdir()
    journal(directory, [])
    link = tmp_path / "alias"
    link.symlink_to(directory, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        JournalSnapshot(link / "transcript.jsonl")
