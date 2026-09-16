"""Width-aware transcript rendering and scroll state, without a terminal."""

from io import StringIO

from rich.console import Console

from pcode.runtime import Message
from pcode.ui import Transcript, TranscriptControl


def make_view():
    transcript = Transcript(Console(file=StringIO()))
    transcript.full_screen = True
    return transcript, TranscriptControl(transcript)


def plain_lines(content):
    return [
        "".join(fragment[1] for fragment in content.get_line(i)) for i in range(content.line_count)
    ]


def test_markdown_reflows_and_same_width_uses_cache():
    transcript, view = make_view()
    transcript.events((Message("**Start** " + "word " * 40 + "end"),))
    wide = plain_lines(view.create_content(100, 50))
    cached = view.lines
    view.create_content(100, 50)
    assert view.lines is cached
    narrow = plain_lines(view.create_content(35, 50))
    assert len(narrow) > len(wide)
    assert " ".join(" ".join(narrow).split()) == " ".join(" ".join(wide).split())
    assert "**" not in "".join(narrow)


def test_scroll_pauses_following_and_height_resize_preserves_anchor():
    transcript, view = make_view()
    for i in range(100):
        transcript.note(f"line {i}")
    view.create_content(80, 20)
    view.scroll(-1)
    before = plain_lines(view.create_content(80, 20))
    transcript.note("new output")
    assert plain_lines(view.create_content(80, 20)) == before
    view.create_content(80, 70)
    assert plain_lines(view.create_content(80, 20)) == before
    view.latest()
    assert plain_lines(view.create_content(80, 20))[-1] == "new output"


def test_scrolled_width_resize_keeps_current_block():
    transcript, view = make_view()
    for i in range(30):
        transcript.note(f"block {i}: " + "word " * 20)
    view.create_content(100, 10)
    view.scroll(-1)
    view.create_content(100, 10)
    block = max(i for i, start in enumerate(view.block_starts) if start <= view.top)
    view.create_content(40, 10)
    assert max(i for i, start in enumerate(view.block_starts) if start <= view.top) == block
