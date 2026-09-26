"""Fingerprints must name what moved a prefix, and never carry prompt text."""

import asyncio
import json
import warnings
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import (
    CachePoint,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RequestUsage
from test_cache_warnings import collect, runtime_for

from pcode.cache_diagnostics import (
    CacheDiagnostics,
    divergence,
    fingerprint,
    fingerprint_message,
)
from pcode.cache_warnings import CacheBustReporting
from pcode.runtime import CacheBust

SECRET = "synthetic-prompt-body-that-must-not-be-logged"


def dumping(usages):
    """A runtime whose notices write fingerprints, as the `debug` setting enables."""
    return runtime_for(usages, monitor=CacheBustReporting(dump_fingerprints=True))


def tool(name="noop", schema=None):
    return ToolDefinition(
        name=name, parameters_json_schema=schema or {"type": "object"}, description="d"
    )


def context(messages, *, instructions="instructions", tools=(), settings=None):
    return SimpleNamespace(
        model_request_parameters=SimpleNamespace(
            instruction_parts=[SystemPromptPart(content=instructions)],
            function_tools=list(tools),
            output_tools=[],
        ),
        model_settings=settings,
        messages=list(messages),
    )


def reply(read=0, write=0):
    return ModelResponse(
        parts=[TextPart("x")],
        usage=RequestUsage(input_tokens=10, cache_read_tokens=read, cache_write_tokens=write),
        provider_name="test",
        model_name="test",
    )


def print_for(messages, **kwargs):
    return fingerprint(context(messages, **kwargs), reply(), step=1)


def turn(text=SECRET):
    return [
        ModelRequest(parts=[UserPromptPart(content=text)]),
        ModelResponse(parts=[ToolCallPart("noop", {"a": 1}, tool_call_id="c")]),
        ModelRequest(parts=[ToolReturnPart("noop", "result", tool_call_id="c")]),
    ]


def test_identical_requests_fingerprint_identically():
    assert print_for(turn()).messages == print_for(turn()).messages


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.__setitem__(0, ModelRequest(parts=[UserPromptPart(content="other")])),
        lambda m: m.__setitem__(
            1, ModelResponse(parts=[ToolCallPart("noop", {"a": 2}, tool_call_id="c")])
        ),
        lambda m: m.__setitem__(
            2, ModelRequest(parts=[ToolReturnPart("noop", "changed", tool_call_id="c")])
        ),
    ],
)
def test_any_edited_message_changes_only_its_own_digest(mutate):
    before = print_for(turn()).messages
    messages = turn()
    mutate(messages)
    after = print_for(messages).messages
    differing = [i for i in range(len(before)) if before[i].moved(after[i])]
    assert len(differing) == 1


def test_cache_points_are_tracked_and_change_the_digest():
    plain = ModelRequest(parts=[UserPromptPart(content=["text"])])
    anchored = ModelRequest(parts=[UserPromptPart(content=["text", CachePoint(ttl="5m")])])
    assert fingerprint_message(plain).cache_points == 0
    assert fingerprint_message(anchored).cache_points == 1
    assert fingerprint_message(plain).moved(fingerprint_message(anchored))
    assert print_for([anchored]).cache_point_indexes == [0]


def test_appended_history_reports_an_intact_prefix():
    before = print_for(turn())
    after = print_for([*turn(), ModelRequest(parts=[UserPromptPart(content="next")])])
    summary = divergence(before, after)
    assert "Request fingerprints unchanged" in summary
    assert "1 messages appended" in summary
    assert "cause unknown" in summary
    assert "TTL" not in summary and "byte-identical" not in summary


def test_rewritten_middle_message_is_named_with_its_index():
    messages = turn()
    before = print_for([*messages, ModelRequest(parts=[UserPromptPart(content="tail")])])
    messages[1] = ModelResponse(parts=[ToolCallPart("noop", {"a": 9}, tool_call_id="c")])
    after = print_for([*messages, ModelRequest(parts=[UserPromptPart(content="tail")])])
    summary = divergence(before, after)
    assert "Message 1 of 4 changed" in summary
    assert "3 prior message(s) from that index" in summary
    assert "Request fingerprints unchanged" not in summary


def test_dropped_history_is_distinguished_from_a_rewrite():
    before = print_for(turn())
    after = print_for(turn()[:2])
    assert "History shrank from 3 to 2" in divergence(before, after)


def test_instruction_and_tool_changes_outrank_message_diffs():
    base = print_for(turn(), instructions="a", tools=[tool()])
    grown = print_for(turn("other"), instructions="a much longer instruction", tools=[tool()])
    assert "Instructions changed" in divergence(base, grown)

    added = print_for(turn("other"), instructions="a", tools=[tool(), tool("extra")])
    assert "Tool definitions changed (added extra)" in divergence(base, added)

    reordered = print_for(turn(), instructions="a", tools=[tool("extra"), tool()])
    assert "removed" not in divergence(added, reordered)
    assert "edited schema or order" in divergence(added, reordered)


def test_cache_settings_changes_are_reported():
    before = print_for(turn(), settings={"anthropic_cache": "5m"})
    after = print_for(turn(), settings={"anthropic_cache": "1h"})
    assert "Cache settings changed" in divergence(before, after)


def test_window_is_bounded_and_summary_needs_two_requests():
    diagnostics = CacheDiagnostics()
    assert diagnostics.summary() == ""
    for _ in range(40):
        diagnostics.record(context(turn()), reply())
    assert len(diagnostics.records) == diagnostics.records.maxlen
    assert diagnostics.records[-1].step == 40


def test_bust_event_names_the_cause_and_dump_excludes_prompt_text(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("PCODE_CACHE_DIAGNOSTICS", raising=False)

    async def run():
        runtime = dumping([(8000, 0), (0, 0)])
        return [event async for event in runtime.stream(SECRET)]

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        events = asyncio.run(run())

    bust = next(event for event in events if isinstance(event, CacheBust))
    assert "Request fingerprints unchanged" in bust.text or "changed" in bust.text
    assert "Request fingerprints:" in bust.text

    dumps = list((tmp_path / "pcode" / "cache-diagnostics").glob("*.json"))
    assert len(dumps) == 1
    raw = dumps[0].read_text()
    assert SECRET not in raw
    payload = json.loads(raw)
    assert payload["model"] == "test/test"
    assert payload["summary"] == bust.text.rsplit("\n", 1)[0].split("\n", 1)[1]
    assert len(payload["requests"]) == 2
    assert [record["step"] for record in payload["requests"]] == [1, 2]
    assert payload["requests"][0]["cache_read"] == 8000
    assert "pcode" in payload["versions"]
    # Digests and sizes only: no message may carry a content field.
    for record in payload["requests"]:
        for message in record["messages"]:
            assert set(message) == {"kind", "parts", "chars", "digest", "cache_points"}


def test_unreadable_request_degrades_to_silence_without_ending_the_run(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(
        "pcode.cache_warnings.CacheDiagnostics.record",
        lambda self, *args: (_ for _ in ()).throw(RuntimeError("unknown part shape")),
    )

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        events = asyncio.run(collect(runtime_for([(8000, 0), (0, 0)])))

    # The provider's own verdict still reaches the user; only the extra
    # diagnosis is missing, and no stale window invents a divergence.
    bust = next(event for event in events if isinstance(event, CacheBust))
    assert "reused 0 of ~8,000" in bust.text
    assert "Request fingerprints unchanged" not in bust.text and "Message" not in bust.text


@pytest.mark.parametrize("debug", [False, True])
def test_dumps_can_be_disabled_without_losing_the_notice(monkeypatch, tmp_path, debug):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    if debug:
        monkeypatch.setenv("PCODE_CACHE_DIAGNOSTICS", "off")
    else:
        # Without `debug`, nothing is written even where dumps are allowed.
        monkeypatch.delenv("PCODE_CACHE_DIAGNOSTICS", raising=False)
    runtime = dumping([(8000, 0), (0, 0)]) if debug else runtime_for([(8000, 0), (0, 0)])

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        events = asyncio.run(collect(runtime))

    bust = next(event for event in events if isinstance(event, CacheBust))
    assert "reused 0 of ~8,000" in bust.text
    assert "cause unknown" in bust.text
    assert "Request fingerprints:" not in bust.text
    assert not (tmp_path / "pcode" / "cache-diagnostics").exists()


def test_unwritable_dump_directory_does_not_break_the_run(monkeypatch, tmp_path):
    monkeypatch.setenv("PCODE_CACHE_DIAGNOSTICS", str(tmp_path / "blocked" / "dir"))
    (tmp_path / "blocked").write_text("not a directory")

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        events = asyncio.run(collect(dumping([(8000, 0), (0, 0)])))

    bust = next(event for event in events if isinstance(event, CacheBust))
    assert "Request fingerprints:" not in bust.text


def test_each_run_fingerprints_only_its_own_requests(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    async def run():
        runtime = dumping([(8000, 0), (0, 0)])
        await collect(runtime)
        runtime.agent.model.usages = [(8000, 0), (0, 0)]
        runtime.agent.model.step = 0
        await collect(runtime)

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        asyncio.run(run())

    dumps = sorted((tmp_path / "pcode" / "cache-diagnostics").glob("*.json"))
    assert len(dumps) == 2
    for dump in dumps:
        payload = json.loads(dump.read_text())
        # A fresh window per run: never the previous turn's requests as well.
        assert [record["step"] for record in payload["requests"]] == [1, 2]
