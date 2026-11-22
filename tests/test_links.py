import asyncio
from io import StringIO

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.conversation_tree import ConversationTree
from pcode.links import (
    Link,
    conversation_links,
    extract_links,
    file_target,
    is_text_file,
    link_editor_command,
    open_link,
)
from pcode.links_ui import link_rows, links_dialog
from pcode.markdown import Markdown
from pcode.popup_surface import PopupSurface


def test_extract_markdown_labels_bare_urls_and_trailing_punctuation():
    text = (
        "See [the docs](https://example.com/docs) and https://example.com/a?b=1&c=2. "
        "Also (https://en.wikipedia.org/wiki/Foo_(bar)) and <https://x.test/y>, "
        "then https://example.com/docs again."
    )
    assert extract_links(text, "assistant") == [
        Link("https://example.com/a?b=1&c=2", "", "assistant"),
        Link("https://en.wikipedia.org/wiki/Foo_(bar)", "", "assistant"),
        Link("https://x.test/y", "", "assistant"),
        Link("https://example.com/docs", "the docs", "assistant"),
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
        Link("https://a.test", "A", "assistant"),
        Link("https://b.test", "", "assistant"),
    ]
    tree.consume({"kind": "tree_selected", "node_id": "c"})
    assert [link.url for link in conversation_links(tree)] == [
        "https://a.test",
        "https://b.test",
        "https://c.test",
    ]


def test_conversation_links_include_tool_payloads_on_active_path():
    records = [
        {"kind": "turn_started", "run_id": "a", "prompt": "https://prompt.test"},
        {
            "kind": "ToolStarted",
            "name": "get_page",
            "arguments": '{"url": "https://args.test/page?a=1&b=2"}',
        },
        {
            "kind": "ToolSummary",
            "name": "get_page",
            "result": "[Docs](https://result.test) and https://prompt.test",
        },
        {"kind": "Message", "markdown": "https://result.test and https://reply.test"},
        {"kind": "turn_completed"},
        {"kind": "turn_started", "run_id": "b", "prompt": "fork"},
        {"kind": "ToolStarted", "name": "shell", "arguments": None},
        {
            "kind": "ToolSummary",
            "name": "shell",
            "result": None,
            "detail": "https://legacy.test",
            "error": "failed: https://error.test",
        },
        {"kind": "turn_failed"},
    ]
    tree = ConversationTree()
    for record in records:
        tree.consume(record)
    assert conversation_links(tree) == [
        Link("https://args.test/page?a=1&b=2", "", "get_page"),
        Link("https://prompt.test", "", "get_page"),
        Link("https://result.test", "Docs", "assistant"),
        Link("https://reply.test", "", "assistant"),
        Link("https://legacy.test", "", "shell"),
        Link("https://error.test", "", "shell"),
    ]
    tree.consume({"kind": "tree_selected", "node_id": "a"})
    assert len(conversation_links(tree)) == 4
    tree.consume({"kind": "tree_selected", "node_id": None})
    assert conversation_links(tree) == []


def test_tool_argument_links_are_available_before_result():
    tree = ConversationTree()
    tree.consume({"kind": "turn_started", "run_id": "a", "prompt": "fetch"})
    tree.consume({"kind": "ToolStarted", "name": "shell", "command": "curl https://running.test"})
    assert conversation_links(tree) == [Link("https://running.test", "", "shell")]


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


def test_links_dialog_search_and_tool_filter():
    links = [
        Link("https://old.test/path", "Reference Manual", "assistant"),
        Link("https://new.test", "", "shell"),
    ]

    async def run():
        # The picker opens in the search line; Ctrl+T toggles tools from there.
        cases = (
            ("\x14\r", "https://old.test/path"),
            ("\x14\x14\r", "https://old.test/path"),  # Retain selection after toggling.
            ("REFERENCE\r", "https://old.test/path"),
            ("old.test/path\r", "https://old.test/path"),
            ("SHELL\r", "https://new.test"),
            ("t\r", "https://new.test"),  # Typed `t` searches; it does not hide tools.
            ("missing\r\x03", None),  # Empty matches cannot open a stale URL.
            ("\t\x06missing\r\x03", None),  # Ctrl+F in the list returns to the search.
            ("missing\x1b", "https://new.test"),  # Esc clears the search first.
        )
        for keys, expected in cases:
            with create_pipe_input() as pipe:
                dialog = links_dialog(links, input=pipe, output=DummyOutput())
                task = asyncio.create_task(dialog.run_async())
                await asyncio.sleep(0.05)
                pipe.send_text(keys)
                if keys.endswith("\x1b"):
                    await asyncio.sleep(0.6)
                    pipe.send_text("\r")
                assert await asyncio.wait_for(task, 2) == expected

    asyncio.run(run())


def test_links_dialog_tab_moves_from_the_search_line_to_the_list():
    links = [Link("https://old.test"), Link("https://new.test")]

    async def run():
        with create_pipe_input() as pipe:
            dialog = links_dialog(links, input=pipe, output=DummyOutput())
            task = asyncio.create_task(dialog.run_async())
            await asyncio.sleep(0.05)
            # In the list, `j` is not typed into the search, so Enter still opens.
            pipe.send_text("\tj\r")
            assert await asyncio.wait_for(task, 2) == "https://new.test"

    asyncio.run(run())


def test_links_dialog_can_recover_when_tools_are_only_links():
    async def run():
        with create_pipe_input() as pipe:
            dialog = links_dialog(
                [Link("https://tool.test", "", "shell")], input=pipe, output=DummyOutput()
            )
            task = asyncio.create_task(dialog.run_async())
            await asyncio.sleep(0.05)
            pipe.send_text("\x14\r\x14\x14\r\x14\r")
            assert await asyncio.wait_for(task, 2) == "https://tool.test"

    asyncio.run(run())


def test_filter_tools_before_deduplicating_and_sort_by_last_appearance():
    tree = ConversationTree()
    tree.consume({"kind": "turn_started", "run_id": "a", "prompt": "https://shared.test"})
    tree.consume(
        {"kind": "ToolSummary", "name": "shell", "result": "https://tool.test https://shared.test"}
    )
    tree.consume({"kind": "Message", "markdown": "https://reply.test https://shared.test"})
    tree.consume({"kind": "turn_completed"})
    tree.consume({"kind": "turn_started", "run_id": "b", "prompt": "https://reply.test"})
    assert [link.url for link in reversed(conversation_links(tree))] == [
        "https://reply.test",
        "https://shared.test",
        "https://tool.test",
    ]
    assert conversation_links(tree, include_tools=False) == [
        Link("https://shared.test", "", "assistant"),
        Link("https://reply.test", "", "user"),
    ]


def test_link_recency_follows_interleaved_events_and_preserves_message_links():
    tree = ConversationTree()
    tree.consume({"kind": "turn_started", "run_id": "a", "prompt": "fetch"})
    tree.consume({"kind": "Message", "markdown": "https://a.test"})
    tree.consume(
        {
            "kind": "ToolSummary",
            "name": "shell",
            "result": "https://b.test https://a.test https://c.test",
        }
    )
    tree.consume({"kind": "turn_failed"})
    links = conversation_links(tree)
    messages = conversation_links(tree, include_tools=False)
    assert [link.url for link in reversed(links)] == [
        "https://c.test",
        "https://a.test",
        "https://b.test",
    ]
    assert messages == [Link("https://a.test", "", "assistant")]

    async def run():
        with create_pipe_input() as pipe:
            dialog = links_dialog(links, message_links=messages, input=pipe, output=DummyOutput())
            task = asyncio.create_task(dialog.run_async())
            await asyncio.sleep(0.05)
            pipe.send_text("\x14\r")
            assert await asyncio.wait_for(task, 2) == "https://a.test"

    asyncio.run(run())


def test_tool_links_track_last_appearance():
    tree = ConversationTree()
    tree.consume({"kind": "turn_started", "run_id": "a", "prompt": "fetch"})
    for result in ("https://a.test https://b.test", "https://a.test"):
        tree.consume({"kind": "ToolSummary", "name": "shell", "result": result})
    assert [link.url for link in conversation_links(tree)] == ["https://b.test", "https://a.test"]


def test_extract_links_stops_at_escaped_quotes_and_newlines():
    text = r"{\"url\": \"https://example.test/pull/505\"} https://other.test\nnext"
    assert extract_links(text) == [
        Link("https://example.test/pull/505"),
        Link("https://other.test"),
    ]
    assert extract_links("https://example.test/path%5C") == [Link("https://example.test/path%5C")]


def test_extract_links_preserves_mixed_markdown_and_bare_order():
    assert extract_links("https://first.test [Second](https://second.test) https://last.test") == [
        Link("https://first.test"),
        Link("https://second.test", "Second"),
        Link("https://last.test"),
    ]


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


def test_markdown_renders_file_links_as_hyperlinks_and_blocks_unsafe_schemes():
    buf = StringIO()
    console = Console(file=buf, force_terminal=True, color_system="truecolor", width=120)
    console.print(
        Markdown(
            "See [Makefile](file:///tmp/Makefile#L60-L61), "
            "[js](javascript:alert(1)), [vb](vbscript:foo), "
            "[html](data:text/html,hi), and [docs](https://example.com)."
        )
    )
    rendered = buf.getvalue()
    assert "\x1b]8;" in rendered
    assert "file:///tmp/Makefile#L60-L61" in rendered
    assert "[Makefile](" not in rendered
    assert "[js](javascript:alert(1))" in rendered
    assert "[vb](vbscript:foo)" in rendered
    assert "[html](data:text/html,hi)" in rendered
    assert "https://example.com" in rendered


def test_extract_links_includes_file_urls():
    text = (
        "Check [Makefile](file:///Users/aaron/Makefile#L60-L61) + "
        "[SKILL.md](file:///Users/aaron/SKILL.md) and bare file:///tmp/notes.txt."
    )
    assert extract_links(text, "assistant") == [
        Link("file:///Users/aaron/Makefile#L60-L61", "Makefile", "assistant"),
        Link("file:///Users/aaron/SKILL.md", "SKILL.md", "assistant"),
        Link("file:///tmp/notes.txt", "", "assistant"),
    ]


def test_file_target_decodes_paths_and_line_anchors(tmp_path):
    space_file = tmp_path / "my file.py"
    space_file.write_text("print(1)\n")
    assert file_target(f"file://{tmp_path}/my%20file.py#L60-L61") == (space_file, 60)
    assert file_target(f"file://localhost{tmp_path}/my%20file.py#L12C4") == (space_file, 12)
    assert file_target(f"file://{tmp_path}/my%20file.py#line=7") == (space_file, 7)
    assert file_target(f"file://{tmp_path}/my%20file.py#42") == (space_file, 42)
    assert file_target(f"file://{tmp_path}/my%20file.py#1-overview") == (space_file, None)
    assert file_target(f"file://{tmp_path}/my%20file.py#2026-01-changelog") == (space_file, None)
    assert file_target(f"file://{tmp_path}/my%20file.py:19:3") == (space_file, 19)
    assert file_target("https://example.com/file.py#L10") is None
    assert file_target("file://otherhost/tmp/file.py") is None


def test_is_text_file_and_link_editor_command(tmp_path):
    makefile = tmp_path / "Makefile"
    makefile.write_text("test:\n\tpytest\n")
    ts_file = tmp_path / "index.ts"
    ts_file.write_text("export const x: number = 1;\n")
    empty = tmp_path / "empty.txt"
    empty.write_text("")
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.7\n1 0 obj\n")
    gzip_txt = tmp_path / "notes.txt.gz"
    gzip_txt.write_bytes(b"\x1f\x8b\x08\x00fake")
    control_blob = tmp_path / "ctrl.bin"
    control_blob.write_bytes(b"\x01\x02\x03\x04\x05\x06abc")
    non_utf8_blob = tmp_path / "raw.bin"
    non_utf8_blob.write_bytes(bytes(range(1, 256)))
    binary = tmp_path / "blob"
    binary.write_bytes(b"\x7fELF\x00\x01\x02")
    subdir = tmp_path / "dir"
    subdir.mkdir()

    assert is_text_file(makefile)
    assert is_text_file(ts_file)
    assert is_text_file(empty)
    assert not is_text_file(image)
    assert not is_text_file(pdf)
    assert not is_text_file(gzip_txt)
    assert not is_text_file(control_blob)
    assert not is_text_file(non_utf8_blob)
    assert not is_text_file(binary)
    assert not is_text_file(subdir)

    env = {"VISUAL": "nvim"}
    assert link_editor_command(f"{makefile.as_uri()}#L60-L61", environ=env) == [
        "nvim",
        "+60",
        str(makefile),
    ]
    assert link_editor_command(f"{makefile.as_uri()}#L60", environ={"EDITOR": "code"}) == [
        "code",
        "--goto",
        f"{makefile}:60",
    ]
    assert link_editor_command(makefile.as_uri(), environ={"VISUAL": "   "}) == [
        "vi",
        str(makefile),
    ]
    try:
        link_editor_command(makefile.as_uri(), environ={"VISUAL": 'nvim "unterminated'})
    except RuntimeError as error:
        assert "Invalid editor command" in str(error)
    else:
        raise AssertionError("unmatched quote in $VISUAL should raise")
    assert link_editor_command(image.as_uri(), environ=env) is None
    assert link_editor_command(pdf.as_uri(), environ=env) is None
    assert link_editor_command(subdir.as_uri(), environ=env) is None
    assert link_editor_command("https://example.com", environ=env) is None

    try:
        link_editor_command(f"file://{tmp_path}/missing.py", environ=env)
    except RuntimeError as error:
        assert "does not exist" in str(error)
    else:
        raise AssertionError("missing file:// link should raise")


def test_open_link_unwraps_file_url_path(tmp_path, monkeypatch):
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    calls = []
    monkeypatch.setattr("pcode.links.sys.platform", "darwin")
    monkeypatch.setattr("pcode.links.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("pcode.links.subprocess.Popen", lambda argv, **kw: calls.append(argv))

    open_link(f"{image.as_uri()}#L1")
    assert calls == [["open", str(image)]]


def test_choose_link_opens_text_file_in_editor_and_falls_back_for_binary(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from pydantic_ai import Agent

    from pcode.app import PreviewApp
    from pcode.live import AgentRuntime

    text_file = tmp_path / "Makefile"
    text_file.write_text("all:\n\techo hi\n")
    image_file = tmp_path / "shot.png"
    image_file.write_bytes(b"\x89PNG\r\n\x1a\n\x00")

    monkeypatch.setenv("VISUAL", "nvim")
    ran_editor = []
    ran_opener = []
    monkeypatch.setattr("pcode.app.subprocess.run", lambda cmd, **kw: ran_editor.append(cmd))
    monkeypatch.setattr("pcode.links.open_link", lambda url: ran_opener.append(url))

    async def run():
        runtime = AgentRuntime(Agent("test"))
        runtime.tree.consume({"kind": "turn_started", "run_id": "a", "prompt": "check"})
        runtime.tree.consume(
            {
                "kind": "Message",
                "markdown": (
                    f"[Makefile]({text_file.as_uri()}#L2) and [shot]({image_file.as_uri()})"
                ),
            }
        )
        runtime.tree.consume({"kind": "turn_completed"})
        output = StringIO()
        app = PreviewApp(runtime=runtime, console=Console(file=output))

        @asynccontextmanager
        async def fake_popup(_output, _session, **options):
            yield PopupSurface(None, _session.app.output)

        monkeypatch.setattr(app, "popup", fake_popup)
        fake_session = SimpleNamespace(app=SimpleNamespace(output=DummyOutput(), style=None))

        picked = iter([f"{text_file.as_uri()}#L2", image_file.as_uri()])

        class FakeDialog:
            async def run_async(self):
                return next(picked)

        monkeypatch.setattr("pcode.links_ui.links_dialog", lambda *a, **kw: FakeDialog())

        await app.choose_link(output=None, session=fake_session)
        await app.choose_link(output=None, session=fake_session)

    asyncio.run(run())
    assert ran_editor == [["nvim", "+2", str(text_file)]]
    assert ran_opener == [image_file.as_uri()]
