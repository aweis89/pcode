import asyncio
from io import StringIO

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.conversation_tree import ConversationTree
from pcode.copy_ui import Snippet, last_response, snippets

RESPONSE = """Two things:

- Report it and paste:
  > SAML login fails for
  > **all** users.
  lazy continuation

```bash
zed schema read
```

> outer
> > nested
"""


def test_snippets_pull_top_level_quotes_and_code_without_markers():
    found = snippets(RESPONSE)
    assert found[0] == Snippet("response", RESPONSE)
    assert found[1:] == [
        Snippet("quote", "SAML login fails for\n**all** users.\nlazy continuation"),
        # No trailing newline, which would run the command when pasted in a shell.
        Snippet("code", "zed schema read", "bash"),
        Snippet("quote", "outer\n> nested"),
    ]
    assert [s.label for s in found] == ["Whole response", "Quote", "Code (bash)", "Quote"]
    assert snippets("just prose") == [Snippet("response", "just prose")]
    assert snippets("  \n") == []


def tree_with(*responses: str) -> ConversationTree:
    tree = ConversationTree()
    parent = None
    for index, response in enumerate(responses):
        identity = f"t{index}"
        tree.consume(
            {"kind": "turn_started", "run_id": identity, "parent_id": parent, "prompt": "ask"}
        )
        if response:
            tree.consume({"kind": "Message", "markdown": response})
        tree.consume({"kind": "turn_completed"})
        parent = identity
    return tree


def test_last_response_skips_turns_without_text():
    assert last_response(tree_with("first", "")) == "first"
    assert last_response(ConversationTree()) == ""


def test_copy_command_copies_a_plain_response_without_a_picker(monkeypatch):
    from pydantic_ai import Agent

    import pcode.clipboard
    from pcode.app import PreviewApp
    from pcode.live import AgentRuntime

    copied: list[str] = []
    monkeypatch.setattr(
        pcode.clipboard, "copy", lambda text, output=None: (copied.append(text), (True, False))[1]
    )

    async def run():
        runtime = AgentRuntime(Agent("test"))
        output = StringIO()
        app = PreviewApp(runtime=runtime, console=Console(file=output))
        assert app.registry.dispatch("/copy")
        assert app.copy_requested
        await app.choose_copy(output=None, session=None)
        assert not app.copy_requested and "No response to copy" in output.getvalue()
        runtime.tree = tree_with("plain answer")
        await app.choose_copy(output=None, session=None)
        assert copied == ["plain answer"] and "Copied response." in output.getvalue()

    asyncio.run(run())


def test_tree_copy_opens_a_picker_for_quotes(monkeypatch):
    import pcode.tree_ui as tree_ui
    from pcode.tree_ui import TreeBrowser

    copied: list[str] = []
    monkeypatch.setattr(
        tree_ui,
        "copy_to_clipboard",
        lambda text, output=None: (copied.append(text), (True, False))[1],
    )
    with create_pipe_input() as pipe:
        browser = TreeBrowser(tree_with(RESPONSE), input=pipe, output=DummyOutput())
        browser.copy()
        assert browser.picker is not None and not copied
        # The picker starts on the first quote, the reason it opened.
        assert browser.picker.selected().kind == "quote"
        browser.picker.list.buffer.cursor_down()
        chosen = browser.picker.selected()
        assert chosen.kind == "code"
        browser.picker.on_pick(chosen)
        assert browser.picker is None
        assert copied == ["zed schema read"] and browser.notice == "Copied code"


def test_ctrl_y_with_an_empty_editor_copies_the_last_response():
    from pcode.commands import CommandRegistry
    from pcode.ui import create_prompt

    asked: list[bool] = []

    async def run():
        with create_pipe_input() as pipe:
            prompt = create_prompt(
                CommandRegistry(),
                on_submit=lambda text: None,
                on_copy_response=lambda: asked.append(True),
                input=pipe,
                output=DummyOutput(),
            )

            async def feed():
                pipe.send_text("\x19")
                while not asked:
                    await asyncio.sleep(0.01)
                prompt.app.exit(result="")

            task = asyncio.ensure_future(feed())
            try:
                await asyncio.wait_for(prompt.prompt_async(), timeout=5)
            finally:
                task.cancel()

    asyncio.run(run())
    assert asked == [True]
