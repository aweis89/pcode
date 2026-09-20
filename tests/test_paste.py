import asyncio

from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from pcode.commands import CommandRegistry
from pcode.file_refs import ReferenceLexer
from pcode.paste import MARKER_PATTERN, PastedText
from pcode.ui import create_prompt

BIG = "line one\n" + "x" * 300


def test_short_pastes_are_inserted_verbatim():
    pasted = PastedText()
    assert pasted.collapse("hello") == "hello"
    assert pasted.expand("hello") == "hello"


def test_large_paste_collapses_and_expands():
    pasted = PastedText()
    placeholder = pasted.collapse(BIG)
    assert placeholder.startswith("line one⏎xxx")
    assert placeholder.endswith(f"…[+{len(BIG) - 60:,} chars]")
    assert "\n" not in placeholder
    assert pasted.expand(f"explain {placeholder} please") == f"explain {BIG} please"


def test_edited_placeholder_is_left_alone():
    pasted = PastedText()
    placeholder = pasted.collapse(BIG)
    edited = placeholder.replace("line one", "line two")
    assert pasted.expand(edited) == edited


def test_marker_gets_its_own_style():
    placeholder = PastedText().collapse(BIG)
    lexer = ReferenceLexer(extra=[(MARKER_PATTERN, "class:paste-marker")])
    fragments = lexer.lex_document(Document(f"see @app.py {placeholder}"))(0)
    styles = {text: style for style, text in fragments if style}
    assert styles["@app.py"] == "class:reference"
    assert styles[MARKER_PATTERN.search(placeholder).group()] == "class:paste-marker"


def test_prompt_collapses_paste_and_sends_full_text():
    async def run():
        with create_pipe_input() as pipe:
            prompt = create_prompt(CommandRegistry(), input=pipe, output=DummyOutput())

            async def feed():
                pipe.send_text("explain \x1b[200~" + BIG + "\x1b[201~")
                while "chars]" not in prompt.default_buffer.text:
                    await asyncio.sleep(0.01)
                assert "\n" not in prompt.default_buffer.text
                assert len(prompt.default_buffer.text) < 100
                pipe.send_text("\r")

            task = asyncio.ensure_future(feed())
            try:
                return await asyncio.wait_for(prompt.prompt_async(), timeout=5)
            finally:
                task.cancel()

    assert asyncio.run(run()) == "explain " + BIG
