"""Kitty Graphics Protocol smooth spinners and fallback to Rich spinners."""

import asyncio
import os
import pty
import select
import sys

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from pcode import glyphs, kitty_spinner, theme
from pcode.kitty_spinner import PLACEHOLDER, SLOTS, KittySpinners
from pcode.startup_input import TerminalReplyParser
from pcode.tool_panel import panel_fragments
from pcode.ui import PALETTES, Activity, PromptLayout, StatusLine, status_spinner

OK_REPLY = b"\x1b_Gi=31;OK\x1b\\"


@pytest.mark.parametrize(
    "env, asked",
    [
        ({"TERM": "xterm-ghostty", "COLORTERM": "truecolor"}, True),
        ({"TERM_PROGRAM": "ghostty", "COLORTERM": "truecolor"}, True),
        ({"TERM": "xterm-kitty", "COLORTERM": "truecolor"}, True),
        ({"KITTY_WINDOW_ID": "1", "COLORTERM": "truecolor"}, True),
        ({"TERM": "xterm-ghostty", "TMUX": "/tmp/tmux-1/default"}, False),
        ({"TERM": "xterm-kitty", "STY": "1234.pts"}, False),
        ({"TERM": "xterm-ghostty", "NO_COLOR": "1"}, False),
        ({"TERM": "xterm-256color", "COLORTERM": "truecolor"}, False),
        ({"TERM_PROGRAM": "WezTerm", "COLORTERM": "truecolor"}, False),
    ],
)
def test_worth_asking_gates_on_ghostty_or_kitty_truecolor_outside_multiplexer(env, asked):
    assert kitty_spinner.worth_asking(env) is asked


def _probe(monkeypatch, *, env, reply: bytes, probe_kitty: bool = True, timeout: float = 0.2):
    import threading

    master, slave = pty.openpty()
    monkeypatch.setattr(sys, "stdin", os.fdopen(slave, "r", closefd=False))
    monkeypatch.setattr(sys, "stdout", os.fdopen(slave, "w", closefd=False))
    for key in ("TERM", "TERM_PROGRAM", "COLORTERM", "TMUX", "STY", "NO_COLOR", "KITTY_WINDOW_ID"):
        monkeypatch.delenv(key, raising=False)
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    monkeypatch.setattr(theme, "_kitty_graphics", None)
    theme._pending_input.clear()
    sent = bytearray()

    def peer():
        while select.select([master], [], [], 0.15)[0]:
            chunk = os.read(master, 4096)
            if not chunk:
                break
            sent.extend(chunk)
            if b"\x1b]11;?" in sent:
                os.write(master, reply)
                break

    thread = threading.Thread(target=peer, daemon=True)
    thread.start()
    try:
        theme._query_background(timeout, probe_kitty=probe_kitty)
        thread.join(timeout=0.5)
        return bytes(sent), bytes(theme._pending_input)
    finally:
        sys.stdin.close()
        sys.stdout.close()
        os.close(slave)
        os.close(master)
        theme._pending_input.clear()


def test_startup_probe_enables_kitty_graphics_on_ok_reply(monkeypatch):
    sent, pending = _probe(
        monkeypatch,
        env={"TERM": "xterm-ghostty", "COLORTERM": "truecolor"},
        reply=OK_REPLY + b"\x1b]11;rgb:1111/1111/1111\x1b\\",
    )
    assert kitty_spinner.QUERY in sent
    assert theme.kitty_graphics_supported() is True
    assert pending == b""


def test_startup_probe_falls_back_when_terminal_does_not_reply(monkeypatch):
    sent, _ = _probe(
        monkeypatch,
        env={"TERM": "xterm-ghostty", "COLORTERM": "truecolor"},
        reply=b"\x1b]11;rgb:1111/1111/1111\x1b\\",
    )
    assert kitty_spinner.QUERY in sent
    assert theme.kitty_graphics_supported() is False


def test_startup_probe_skips_kitty_query_when_unasked_or_unsupported(monkeypatch):
    sent, _ = _probe(
        monkeypatch,
        env={"TERM": "xterm-256color", "COLORTERM": "truecolor"},
        reply=b"\x1b]11;rgb:1111/1111/1111\x1b\\",
        probe_kitty=True,
    )
    assert kitty_spinner.QUERY not in sent
    assert theme.kitty_graphics_supported() is False


def test_late_kitty_reply_is_filtered_from_typing():
    received: list[str] = []
    parser = TerminalReplyParser(lambda key: received.append(key.data))
    parser.feed("a\x1b_Gi=31;OK\x1b\\b")
    parser.flush()
    assert "".join(received) == "ab"


def test_smooth_style_selection_and_rich_fallback(monkeypatch):
    assert kitty_spinner.smooth_style_for("spinner", "dots", "dots") == "swept"
    assert kitty_spinner.smooth_style_for("tool_spinner", "arc", "arc") == "dual"
    assert kitty_spinner.smooth_style_for("spinner", "orbit", "dots") == "orbit"
    # Explicit non-default Rich spinner is respected instead of overridden.
    assert kitty_spinner.smooth_style_for("spinner", "line", "dots") is None
    assert kitty_spinner.rich_fallback("orbit") == "dots"
    assert kitty_spinner.rich_fallback("dual") == "arc"
    assert kitty_spinner.rich_fallback("line") == "line"

    # When a smooth name is configured on a non-Kitty terminal, status_spinner falls back to Rich.
    monkeypatch.setattr("pcode.ui.load_preferences", lambda: {"spinner": "comet"})
    monkeypatch.setattr(glyphs, "active", lambda: False)
    sp = status_spinner("spinner")
    assert list(sp.frames) == list("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")


def test_resolve_rgb_handles_hex_and_theme_fallback():
    assert kitty_spinner.resolve_rgb("#c4b5fd", "#000000") == (0xC4, 0xB5, 0xFD)
    assert kitty_spinner.resolve_rgb("nodim #006b80 bold", "#000000") == (0x00, 0x6B, 0x80)
    # ANSI names (`/syntax terminal`) use the theme-matched fallback hex for contrast.
    assert kitty_spinner.resolve_rgb("ansimagenta", "#7c3aed") == (0x7C, 0x3A, 0xED)
    assert kitty_spinner.resolve_rgb("fg:default dim", "#586575") == (0x58, 0x65, 0x75)


def test_kitty_spinners_upload_sync_recolor_and_close():
    async def run():
        ks = KittySpinners(status_style="swept", tool_style="dual", enabled=True)
        writes: list[str] = []
        flushes = 0
        invalidates = 0

        def write_raw(data: str) -> None:
            writes.append(data)

        def flush() -> None:
            nonlocal flushes
            flushes += 1

        def invalidate() -> None:
            nonlocal invalidates
            invalidates += 1

        assert ks.ready is False
        assert ks.active_for("status") is False

        # First sync schedules background preparation; nothing written yet.
        ks.sync(
            PALETTES["dark"],
            PALETTES["dark"],
            write_raw=write_raw,
            flush=flush,
            invalidate=invalidate,
        )
        assert ks._task is not None
        await ks._task
        assert invalidates == 1
        assert writes == []  # Deferred until before_render owns the main screen.

        # Next before_render flushes the prepared payload atomically.
        ks.sync(
            PALETTES["dark"],
            PALETTES["dark"],
            write_raw=write_raw,
            flush=flush,
            invalidate=invalidate,
        )
        assert ks.ready is True
        assert ks.active_for("status") is True
        assert ks.active_for("tool") is True
        assert ks.active_for("accent") is True
        assert ks.active_for("aside") is True
        assert ks.active_for("startup") is True
        assert flushes == 1
        # Payload frees old frames (d=I), uploads every slot, and starts looping (s=3, v=1).
        payload = writes[-1]
        for img_id in SLOTS.values():
            assert f"a=d,d=I,i={img_id},q=2" in payload
            assert f"i={img_id}" in payload
            assert f"a=a,i={img_id},r=1," in payload

        # Same palette is a no-op.
        ks.sync(
            PALETTES["dark"],
            PALETTES["dark"],
            write_raw=write_raw,
            flush=flush,
            invalidate=invalidate,
        )
        assert len(writes) == 1

        # Switching to light palette prepares and flushes a re-upload.
        ks.sync(
            PALETTES["light"],
            PALETTES["light"],
            write_raw=write_raw,
            flush=flush,
            invalidate=invalidate,
        )
        assert ks._task is not None
        await ks._task
        assert invalidates == 2
        ks.sync(
            PALETTES["light"],
            PALETTES["light"],
            write_raw=write_raw,
            flush=flush,
            invalidate=invalidate,
        )
        assert len(writes) == 2

        # Closing deletes every uploaded image with d=I and tolerates OSError.
        ks.close(write_raw, flush)
        assert ks.ready is False
        assert ks.enabled is False
        cleanup = writes[-1]
        for img_id in SLOTS.values():
            assert f"\x1b_Ga=d,d=I,i={img_id},q=2\x1b\\" in cleanup

    asyncio.run(run())


def test_fragments_and_layout_with_kitty_spinners_and_fallback():
    ks = KittySpinners(status_style="swept", tool_style="dual", enabled=True)
    # Not ready yet -> falls back to Rich spinner.
    with create_pipe_input() as pipe:
        session = PromptSession(input=pipe, output=DummyOutput())
    activity = Activity()
    layout = PromptLayout(session, activity, None, None, kitty_spinners=ks)
    assert isinstance(layout.status_spinner_mark(), str)
    assert layout.refresh_interval() < 0.2

    # Mark ready -> emits Kitty placeholder fragments and relaxes refresh_interval.
    ks.ready = True
    ks._uploaded_slots = set(SLOTS)
    assert layout.status_spinner_mark() == ("class:kitty.spinner.status", PLACEHOLDER)
    assert layout.tool_spinner_mark() == ("class:kitty.spinner.tool", PLACEHOLDER)
    assert layout.aside_spinner_mark() == ("class:kitty.spinner.aside", PLACEHOLDER)
    assert layout.wait_spinner_mark() == ("class:kitty.spinner.accent", PLACEHOLDER)
    assert layout.plan_spinner_icon() == PLACEHOLDER
    assert layout.startup_spinner_mark(0.0) == ("class:kitty.spinner.startup", PLACEHOLDER)
    assert layout.refresh_interval() == 0.5

    # StatusLine preserves the dedicated Kitty style class on the placeholder cell only.
    frags = StatusLine("Thinking").fragments(ks.fragment("status"), 40)
    assert frags[0] == ("class:kitty.spinner.status", PLACEHOLDER)
    assert frags[1] == ("class:activity.spinner", " ")

    # Plan row splits the placeholder icon onto `class:kitty.spinner.accent`,
    # even when truncated to narrow widths (`"\U0010eeee…"`).
    plan_frags = panel_fragments([("class:plan.in_progress", f"{PLACEHOLDER} Build feature")], 40)
    assert ("class:kitty.spinner.accent", PLACEHOLDER) in plan_frags
    narrow_frags = panel_fragments([("class:plan.in_progress", f"{PLACEHOLDER} Build feature")], 2)
    assert narrow_frags[0] == ("class:kitty.spinner.accent", PLACEHOLDER)

    # Wait row keeps the placeholder on its own fragment.
    activity.begin_wait("Connecting")
    activity.waits[0].started -= 5.0
    wait_frags = activity.wait_fragments(ks.fragment("accent"), 40)
    assert wait_frags[0] == ("class:kitty.spinner.accent", PLACEHOLDER)
    assert wait_frags[1][0] == "class:activity.system"

    # Prompt style defines every Kitty spinner slot class with its 24-bit fg ID.
    style_dict = dict(PALETTES["dark"].prompt_style().style_rules)
    for slot, img_id in SLOTS.items():
        assert f"fg:#{img_id:06x}" in style_dict[f"kitty.spinner.{slot}"]
