"""A malformed tool call costs a correction, not the turn."""

import asyncio

import pytest
from pydantic import BaseModel, ValidationError
from pydantic_ai.exceptions import ModelRetry, UnexpectedModelBehavior
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from pcode.agent import create_agent, tool_retries
from pcode.live import error_message
from pcode.preferences import save_preferences


class Replacement(BaseModel):
    old_text: str


def validation_error() -> ValidationError:
    try:
        Replacement.model_validate({"old_text": ["not", "a", "string"]})
    except ValidationError as error:
        return error
    raise AssertionError("expected a ValidationError")


def ceiling(cause: Exception, tool: str = "edit_file", limit: int = 1) -> Exception:
    error = UnexpectedModelBehavior(
        f"Tool {tool!r} exceeded max retries count of {limit}. Consider raising the retry "
        "limit, or see the docs on tool retries: https://pydantic.dev/docs/ai/tools-toolsets/"
    )
    error.__cause__ = cause
    return error


def test_budget_follows_the_saved_default():
    assert tool_retries() == {"tools": 3}
    save_preferences(tool_retries="0")
    assert tool_retries() == {"tools": 0}


def test_agents_use_the_budget_and_keep_output_retries_strict(tmp_path, monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    agent = create_agent("test", tmp_path)
    # Private attributes: Pydantic AI exposes no public reader for the resolved
    # budgets, and the distinction between them is the point of the setting.
    assert agent._max_tool_retries == 3
    assert agent._max_output_retries == 1


def test_a_second_malformed_call_is_corrected_rather_than_fatal(tmp_path, monkeypatch):
    """The failure this replaces: two bad `replacements` arrays ended the run."""
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    (tmp_path / "file.txt").write_text("before\n")
    calls = 0

    async def respond(messages, info):
        nonlocal calls
        calls += 1
        if calls <= 2:
            # `replacements` as a string, exactly the shape that aborted the turn.
            mangled = '{"path": "file.txt", "replacements": "[{\\"old_text\\">\\"before\\""}'
            yield {0: DeltaToolCall(name="edit_file", json_args=mangled)}
        elif calls == 3:
            yield {
                0: DeltaToolCall(
                    name="edit_file",
                    json_args='{"path": "file.txt", "old_text": "before", "new_text": "after"}',
                )
            }
        else:
            yield "Edited."

    agent = create_agent("test", tmp_path)
    result = agent.run_sync("fix it", model=FunctionModel(stream_function=respond))
    assert result.output == "Edited."
    assert (tmp_path / "file.txt").read_text() == "after\n"


def test_exhausting_the_budget_still_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    save_preferences(tool_retries="1")

    async def respond(messages, info):
        yield {0: DeltaToolCall(name="edit_file", json_args='{"path": 5}')}

    agent = create_agent("test", tmp_path)
    with pytest.raises(UnexpectedModelBehavior) as caught:
        agent.run_sync("fix it", model=FunctionModel(stream_function=respond))
    message = error_message(caught.value)
    assert "`edit_file`" in message
    assert "retry limit (1)" in message
    assert "tool_retries" in message
    # The turn did not end because of the provider; say so.
    assert "credentials" not in message
    assert "connectivity" not in message


def test_message_names_fields_but_never_their_values():
    message = error_message(ceiling(validation_error()))
    assert "failed validation" in message
    assert "Rejected argument: old_text." in message
    assert "not" not in message.split("Rejected argument")[0].replace("Nothing", "")
    assert "list" not in message


def test_model_retry_cause_reads_differently():
    message = error_message(ceiling(ModelRetry("blocked by a guard"), tool="shell", limit=3))
    assert "a call the tool rejected" in message
    assert "`shell`" in message
    assert "blocked by a guard" not in message


def test_other_unexpected_behavior_keeps_the_generic_message():
    plain = UnexpectedModelBehavior("Received empty model response")
    plain.__cause__ = RuntimeError("boom")
    assert error_message(plain).startswith("Run failed (UnexpectedModelBehavior).")
    # A ceiling message without a cause cannot be attributed to a tool either.
    assert error_message(
        UnexpectedModelBehavior("Tool 'x' exceeded max retries count of 1")
    ).startswith("Run failed")


def test_runtime_reports_it_through_a_live_stream(tmp_path, monkeypatch):
    from pcode.live import AgentRuntime

    monkeypatch.delenv("EXA_API_KEY", raising=False)
    save_preferences(tool_retries="0")

    async def respond(messages, info):
        yield {0: DeltaToolCall(name="edit_file", json_args='{"path": 5}')}

    runtime = AgentRuntime(create_agent("test", tmp_path))
    runtime.agent.model = FunctionModel(stream_function=respond)

    async def run():
        with pytest.raises(UnexpectedModelBehavior) as caught:
            _ = [event async for event in runtime.stream("fix it")]
        return caught.value

    assert "`edit_file`" in error_message(asyncio.run(run()))
