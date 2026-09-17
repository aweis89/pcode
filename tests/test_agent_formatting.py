"""Formatting guidance reaches the model alongside repository instructions."""

from pydantic_ai.models.function import FunctionModel

from pcode.agent import create_agent


def test_agent_receives_markdown_guidance_on_each_turn(tmp_path, monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    (tmp_path / "AGENTS.md").write_text("Repository-specific guidance marker")
    seen = []

    async def respond(messages, info):
        instructions = info.instructions
        assert "terminal with Markdown rendering and syntax highlighting" in instructions
        assert "fenced code blocks with a language tag" in instructions
        assert "inline backticks for identifiers and short commands" in instructions
        assert "Close all code fences." in instructions
        assert "Write ordinary prose outside code blocks." in instructions
        assert "Repository-specific guidance marker" in instructions
        seen.append(instructions)
        yield "Done"

    agent = create_agent("test", tmp_path)
    model = FunctionModel(stream_function=respond)
    first = agent.run_sync("Show a code example", model=model)
    agent.run_sync("Another example", model=model, message_history=first.all_messages())
    assert len(seen) == 2
