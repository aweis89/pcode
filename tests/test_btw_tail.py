"""The drawer's viewer (`pcode.btw_tail`): one /btw thread drawn as the swap draws it."""

from io import StringIO

import pytest
from rich.console import Console

from pcode.btw_tail import CLEAR, Tail, main
from pcode.popup_hints import hints_apc
from pcode.ui import Activity, Transcript


def tail(thread="t1", *, native_hints=True) -> tuple[Tail, StringIO]:
    out = StringIO()
    console = Console(file=out, width=60, color_system=None, force_terminal=True)
    transcript = Transcript(console, activity=Activity(), detected_theme="dark")
    view = Tail(thread, console, transcript)
    # As a terminal that draws the drawer's key footer says (`PCODE_NATIVE_HINTS`).
    view.native_hints = native_hints
    return view, out


def state(**fields) -> dict:
    base = {"id": "a1", "question": "why?", "thread": "t1", "status": "running", "answer": ""}
    return base | fields


def notify(view: Tail, out: StringIO, method: str, *args) -> str:
    """What applying one host notification writes."""
    out.seek(0)
    out.truncate()
    view.apply({"type": "notify", "method": method, "args": list(args)})
    return out.getvalue()


def test_a_thread_streams_in_block_by_block_and_redraws_when_written_text_changes():
    view, out = tail()
    other = state(id="b2", thread="t2", question="other")
    first = notify(view, out, "asides", [state(activity="Waiting for model…"), other])
    assert first.startswith(CLEAR) and "why?" in first and "other" not in first
    assert "Waiting for model…" in first and "……" not in first
    streamed = notify(view, out, "aside_changed", state(answer="One.\n\nTw"))
    assert "One." in streamed and "Tw" not in streamed and CLEAR not in streamed
    done = notify(view, out, "aside_changed", state(answer="One.\n\nTwo.", status="answered"))
    assert "Two." in done and "Waiting" not in done
    changed = notify(view, out, "aside_changed", state(answer="Changed.", status="answered"))
    assert changed.startswith(CLEAR) and "Changed." in changed
    assert "The session host stopped." in notify(view, out, "host_closed")


def test_the_drawer_footer_lists_the_editor_keys_and_is_sent_only_when_it_changes():
    view, out = tail()
    one = notify(view, out, "asides", [state(status="answered")])
    assert one.endswith(hints_apc(view.footer()).decode())
    labels = [hint.label for hint in view.footer()]
    assert labels == ["Reply in editor", "Close"], "one thread: nothing to go to next"
    again = notify(view, out, "aside_changed", state(answer="More.", status="answered"))
    assert "pcode;hints" not in again
    two = notify(view, out, "asides", [state(status="answered"), state(id="b2", thread="t2")])
    assert "pcode;hints" in two
    assert [hint.label for hint in view.footer()] == ["Reply in editor", "Next thread", "Close"]


def test_without_a_drawn_footer_the_keys_stay_under_the_heading():
    view, out = tail(native_hints=False)
    drawn = notify(view, out, "asides", [state(status="answered")])
    assert "pcode;hints" not in drawn
    assert "Esc back to it" in drawn


def test_only_a_host_id_is_taken():
    with pytest.raises(SystemExit):
        main(["../../etc", "t1"])


def test_host_text_reaches_the_drawer_without_controls_and_the_status_keeps_to_one_row():
    view, out = tail()
    hostile = "why?\x1b]52;c;aGk=\x1b\\\x1b_pcode;focus\x1b\\"
    long_activity = "Searching the web for something rather long indeed " * 3
    drawn = notify(view, out, "asides", [state(question=hostile, activity=long_activity)])
    # The drawer's footer is pcode's own sequence, sent after the frame.
    footer = hints_apc(view.footer()).decode()
    assert drawn.endswith(footer)
    drawn = drawn.removesuffix(footer)
    assert "\x1b]" not in drawn and "\x1b_" not in drawn, "controls are stripped to text"
    status = drawn.rsplit("\n", 1)[-1]
    assert "Searching" in status and "…" in status, "the last row is the whole status"
    errored = state(question="why?", status="failed", error="boom\x1b]0;title\x07")
    assert "\x1b]0;" not in notify(view, out, "aside_changed", errored)


class Frames:
    """A loop stand-in that holds the next paced frame for the test to run."""

    def __init__(self) -> None:
        self.due = None

    def call_later(self, _delay, callback):
        self.due = callback
        return self

    def cancel(self) -> None:
        self.due = None

    def run(self, out: StringIO) -> str:
        """Run every frame until the queue drains; what each wrote."""
        written = []
        while self.due is not None:
            out.seek(0)
            out.truncate()
            frame, self.due = self.due, None
            frame()
            written.append(out.getvalue())
        return written


def paced_tail(mode: str) -> tuple[Tail, StringIO, Frames]:
    out = StringIO()
    console = Console(file=out, width=60, color_system=None, force_terminal=True)
    transcript = Transcript(console, activity=Activity(), detected_theme="dark")
    transcript.paced_scrollback = mode
    frames = Frames()
    return Tail("t1", console, transcript, frames), out, frames


def test_settled_prose_types_out_over_frames_under_the_status_row():
    view, out, frames = paced_tail("typed")
    notify(view, out, "asides", [state(activity="Answering")])
    sentence = "A settled sentence that is long enough to take several frames."
    first = notify(view, out, "aside_changed", state(answer=f"{sentence}\n\nNext"))
    assert sentence[:9] in first and sentence[:30] not in first, "only a step is shown"
    assert first.rstrip().endswith("Answering…"), "the status row stays under it"
    written = frames.run(out)
    assert len(written) > 3, "the rest types out frame by frame"
    assert all(frame.rstrip().endswith("Answering…") for frame in written)
    assert "settled sentence" in "".join(written) and "Next" not in "".join(written)
    assert view.rows == []


def test_rows_roll_in_and_off_writes_at_once():
    answer = "\n\n".join(f"Paragraph {n}." for n in range(40)) + "\n\nTail"
    view, out, frames = paced_tail("rows")
    notify(view, out, "asides", [state()])
    first = notify(view, out, "aside_changed", state(answer=answer))
    assert "Paragraph 0." in first and "Paragraph 38." not in first
    assert "Paragraph 38." in "".join(frames.run(out))

    view, out, frames = paced_tail("off")
    notify(view, out, "asides", [state()])
    assert "Paragraph 38." in notify(view, out, "aside_changed", state(answer=answer))
    assert frames.due is None


def test_a_whole_redraw_lands_at_once_and_drops_the_queue():
    view, out, frames = paced_tail("typed")
    notify(view, out, "asides", [state()])
    notify(view, out, "aside_changed", state(answer="Some prose that types out slowly.\n\nx"))
    assert view.rows
    redrawn = notify(view, out, "aside_changed", state(answer="Changed.", status="answered"))
    assert redrawn.startswith(CLEAR) and "Changed." in redrawn and view.rows == []


def test_a_typed_row_keeps_its_escapes_and_cuts_on_visible_characters():
    from pcode.btw_tail import _typed_part

    link = "\x1b]8;id=1;https://example.com\x1b\\"
    row = f"\x1b[1mBold\x1b[0m {link}link\x1b]8;;\x1b\\ tail\n"
    assert _typed_part(row, 7) == f"\x1b[1mBold\x1b[0m {link}li"
    assert _typed_part(row, 4) == "\x1b[1mBold"
    assert _typed_part("plain\n", 3) == "pla"


def test_live_rows_are_erased_exactly_and_an_unchanged_frame_writes_nothing():
    from pcode.btw_tail import CLOSE_ROW, ERASE_DOWN

    view, out, frames = paced_tail("typed")
    notify(view, out, "asides", [state(activity="Answering")])
    answer = "A settled sentence that is long enough to take several frames.\n\nx"
    first = notify(view, out, "aside_changed", state(answer=answer, activity="Answering"))
    assert CLOSE_ROW + "\n" in first, "a cut-off row closes its styles and links"
    out.seek(0)
    out.truncate()
    frames.due()
    assert out.getvalue().startswith("\x1b[1A" + ERASE_DOWN), "both live rows taken back"
    # Nothing new, and the frame still pending: nothing to write.
    assert notify(view, out, "aside_changed", state(answer=answer, activity="Answering")) == ""


def test_resized_ignores_height_only_changes_and_redraws_on_width_change():
    view, out, frames = paced_tail("typed")
    notify(view, out, "asides", [state(answer="Hello world.", status="answered")])
    out.seek(0)
    out.truncate()

    # Same width (height-only resize): does not schedule a redraw or clear scrollback.
    view.resized()
    assert frames.due is None
    assert out.getvalue() == ""

    # Width changes: schedules a debounced whole redraw at the new width.
    view.console._width = 80
    view.resized()
    assert frames.due is not None
    written = "".join(frames.run(out))
    assert written.startswith(CLEAR) and "Hello world." in written

    # Subsequent height-only resize at the new width is again a no-op.
    out.seek(0)
    out.truncate()
    view.resized()
    assert frames.due is None
    assert out.getvalue() == ""
