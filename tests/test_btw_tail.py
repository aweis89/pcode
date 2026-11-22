"""The drawer's viewer (`pcode.btw_tail`): one /btw thread drawn as the swap draws it."""

from io import StringIO

import pytest
from rich.console import Console

from pcode.btw_tail import CLEAR, Tail, main
from pcode.ui import Activity, Transcript


def tail(thread="t1") -> tuple[Tail, StringIO]:
    out = StringIO()
    console = Console(file=out, width=60, color_system=None, force_terminal=True)
    transcript = Transcript(console, activity=Activity(), detected_theme="dark")
    return Tail(thread, console, transcript), out


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


def test_only_a_host_id_is_taken():
    with pytest.raises(SystemExit):
        main(["../../etc", "t1"])


def test_host_text_reaches_the_drawer_without_controls_and_the_status_keeps_to_one_row():
    view, out = tail()
    hostile = "why?\x1b]52;c;aGk=\x1b\\\x1b_pcode;focus\x1b\\"
    long_activity = "Searching the web for something rather long indeed " * 3
    drawn = notify(view, out, "asides", [state(question=hostile, activity=long_activity)])
    assert "\x1b]" not in drawn and "\x1b_" not in drawn, "controls are stripped to text"
    status = drawn.rsplit("\n", 1)[-1]
    assert "Searching" in status and "…" in status, "the last row is the whole status"
    errored = state(question="why?", status="failed", error="boom\x1b]0;title\x07")
    assert "\x1b]0;" not in notify(view, out, "aside_changed", errored)
