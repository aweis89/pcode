import asyncio
from io import StringIO

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.conversation_tree import ConversationTree
from pcode.links import Link, conversation_links, extract_links, open_link
from pcode.links_ui import link_rows, links_dialog


def test_extract_markdown_labels_bare_urls_and_trailing_punctuation():
    text = (
        "See [the docs](https://example.com/docs) and https://example.com/a?b=1&c=2. "
        "Also (https://en.wikipedia.org/wiki/Foo_(bar)) and <https://x.test/y>, "
        "then https://example.com/docs again."
    )
    assert extract_links(text, "assistant") == [
        Link("https://example.com/docs", "the docs", "assistant"),
        Link("https://example.com/a?b=1&c=2", "", "assistant"),
        Link("https://en.wikipedia.org/wiki/Foo_(bar)", "", "assistant"),
        Link("https://x.test/y", "", "assistant"),
    ]
    assert extract_links("no links here") == []


def test_conversation_links_follow_active_path_and_dedupe():
    tree = ConversationTree()
    tree.consume({"kind": "turn_started", "run_id": "a", "prompt": "open https://a.test"})
    tree.consume({"kind": "Message", "markdown": "sure: [A](https://a.test) and https://b.test"})
    tree.consume({"kind": "turn_completed"})
    tree.consume({"kind": "turn_started", "run_id": "c", "prompt": "fork https://c.test"})
    tree.consume({"kind": "tree_selected", "node_id": "a"})
    assert conversation_links(tree) == [
        Link("https://a.test", "", "user"),
        Link("https://b.test", "", "assistant"),
    ]
    tree.consume({"kind": "tree_selected", "node_id": "c"})
    assert [link.url for link in conversation_links(tree)] == [
        "https://a.test",
        "https://b.test",
        "https://c.test",
    ]


def test_link_rows_show_source_and_label():
    rows = link_rows([Link("https://a.test", "A", "user"), Link("https://b.test")])
    assert rows == [
        ("https://a.test", "user: A — https://a.test"),
        ("https://b.test", "https://b.test"),
    ]


def test_links_dialog_defaults_to_newest_and_cancels():
    links = [Link("https://old.test"), Link("https://new.test")]

    async def run():
        with create_pipe_input() as pipe:
            cases = (("\r", "https://new.test"), ("\x1b[B\r", "https://old.test"), ("\x1b", None))
            for keys, expected in cases:
                dialog = links_dialog(links, input=pipe, output=DummyOutput())
                task = asyncio.create_task(dialog.run_async())
                await asyncio.sleep(0.05)
                pipe.send_text(keys)
                assert await asyncio.wait_for(task, 2) == expected

    asyncio.run(run())


def test_open_link_uses_platform_opener(monkeypatch):
    calls = []
    monkeypatch.setattr("pcode.links.sys.platform", "darwin")
    monkeypatch.setattr("pcode.links.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("pcode.links.subprocess.Popen", lambda argv, **kw: calls.append(argv))
    open_link("https://a.test")
    assert calls == [["open", "https://a.test"]]

    monkeypatch.setattr("pcode.links.sys.platform", "linux")
    open_link("https://b.test")
    assert calls[-1] == ["xdg-open", "https://b.test"]

    monkeypatch.setattr("pcode.links.shutil.which", lambda name: None)
    try:
        open_link("https://c.test")
    except RuntimeError as error:
        assert "xdg-open" in str(error)
    else:
        raise AssertionError("missing opener should raise")


def test_links_command_requests_picker_and_notes_when_empty():
    from pydantic_ai import Agent

    from pcode.app import PreviewApp
    from pcode.live import AgentRuntime

    async def run():
        runtime = AgentRuntime(Agent("test"))
        output = StringIO()
        app = PreviewApp(runtime=runtime, console=Console(file=output))
        assert app.registry.dispatch("/links")
        assert app.links_requested
        await app.choose_link(output=None, session=None)
        assert not app.links_requested
        assert "No links" in output.getvalue()

    asyncio.run(run())
