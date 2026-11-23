"""Kitty Graphics Protocol smooth spinners and fallback to Rich spinners."""

import asyncio
import os
import pty
import select
import sys
from types import SimpleNamespace

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from pcode import glyphs, kitty_spinner, theme
from pcode.kitty_spinner import PLACEHOLDER, SLOTS, KittySpinners
from pcode.startup_input import TerminalReplyParser
from pcode.tool_panel import panel_fragments, plan_row
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
    from pcode.preferences import canonical, valid_preferences

    assert kitty_spinner.smooth_style_for("spinner", "dots", "dots") == "helix"
    assert kitty_spinner.smooth_style_for("tool_spinner", "arc", "arc") == "dual"
    assert kitty_spinner.smooth_style_for("spinner", "orbit", "dots") == "orbit"
    assert kitty_spinner.smooth_style_for("spinner", "octa", "dots") == "octa"
    assert kitty_spinner.smooth_style_for("spinner", "gyro", "dots") == "gyro"
    # Legacy `diamond` config values map cleanly to `octa` through both
    # preference validation and direct `KittySpinners.from_preferences`.
    assert canonical("spinner", "diamond") == "octa"
    assert canonical("tool_spinner", "diamond") == "octa"
    assert "diamond" not in kitty_spinner.STYLES
    monkeypatch.setattr(kitty_spinner, "true_color", lambda: True)
    monkeypatch.setattr(glyphs, "active", lambda: False)
    migrated = valid_preferences({"spinner": "diamond", "tool_spinner": "diamond"})
    assert migrated == {"spinner": "octa", "tool_spinner": "octa"}
    ks_migrated = KittySpinners.from_preferences(migrated, supported=True)
    assert (ks_migrated.status_style, ks_migrated.tool_style) == ("octa", "octa")
    ks_raw = KittySpinners.from_preferences(
        {"spinner": "diamond", "tool_spinner": "diamond"}, supported=True
    )
    assert (ks_raw.status_style, ks_raw.tool_style) == ("octa", "octa")
    # Explicit non-default Rich spinner is respected instead of overridden.
    assert kitty_spinner.smooth_style_for("spinner", "line", "dots") is None
    assert kitty_spinner.rich_fallback("helix") == "dots"
    assert kitty_spinner.rich_fallback("gyro") == "dots"
    assert kitty_spinner.rich_fallback("octa") == "dots"
    assert kitty_spinner.rich_fallback("orbit") == "dots"
    assert kitty_spinner.rich_fallback("dual") == "arc"
    assert kitty_spinner.rich_fallback("line") == "line"
    assert kitty_spinner.style_cols("helix") == 2
    assert kitty_spinner.style_cols("gyro") == 2
    assert kitty_spinner.style_cols("octa") == 1
    assert kitty_spinner.style_cols("dual") == 1

    # When a smooth name is configured on a non-Kitty terminal, status_spinner falls back to Rich.
    monkeypatch.setattr("pcode.ui.load_preferences", lambda: {"spinner": "comet"})
    sp = status_spinner("spinner")
    assert list(sp.frames) == list("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")


def test_every_smooth_style_renders_valid_unclipped_rgba_frames():
    for name, style_spec in kitty_spinner.STYLES.items():
        for cols in {1, style_spec.cols}:
            w = cols * kitty_spinner.CELL_W
            for is_light in (False, True):
                spec = kitty_spinner.SlotSpec(
                    name, cols, (196, 181, 253), (136, 192, 208), is_light=is_light
                )
                max_edge_alpha = 0
                for frame_idx in (0, style_spec.frames // 4, style_spec.frames // 2):
                    raw = kitty_spinner._render_frame(spec, frame_idx)
                    assert len(raw) == w * kitty_spinner.CELL_H * 4
                    assert any(raw[3::4])  # non-empty alpha channel
                    for y in range(kitty_spinner.CELL_H):
                        left_a = raw[(y * w) * 4 + 3]
                        right_a = raw[(y * w + (w - 1)) * 4 + 3]
                        max_edge_alpha = max(max_edge_alpha, left_a, right_a)
                # Shapes stay clear of hard vertical clipping on the left/right edges.
                assert max_edge_alpha < 50, f"{name} ({cols}x1, light={is_light}): {max_edge_alpha}"


def test_kitty_frame_continuation_chunks_include_af():
    import random
    import re

    # Force a multi-chunk `a=f` payload as well as checking real 2x1 gyro frames.
    noisy = random.Random(0).randbytes(6000)
    multi = kitty_spinner._kitty_chunks(
        "a=f,i=1,f=32,s=56,v=56,o=z,z=30,q=2", noisy, cont="a=f,q=2"
    )
    chunks = re.findall(r"\x1b_G([^;]*);([^\x1b]*)\x1b\\", multi)
    assert len(chunks) >= 2
    for idx, (ctrl, _) in enumerate(chunks):
        fields = dict(item.split("=", 1) for item in ctrl.split(",") if "=" in item)
        assert fields["a"] == "f"
        assert fields["m"] == ("1" if idx < len(chunks) - 1 else "0")

    # Verify every APC chunk in 2x1 gyro has valid m= framing and a=f on continuations.
    spec = kitty_spinner.SlotSpec("gyro", 2, (196, 181, 253), (136, 192, 208), is_light=False)
    payload = kitty_spinner.build_upload_payload({"startup": spec})
    in_frame = False
    for ctrl, _ in re.findall(r"\x1b_G([^;]*);?([^\x1b]*)\x1b\\", payload):
        fields = dict(item.split("=", 1) for item in ctrl.split(",") if "=" in item)
        if in_frame:
            assert fields.get("a") == "f"
            in_frame = fields.get("m") == "1"
        elif fields.get("a") == "f" and fields.get("m") == "1":
            in_frame = True
    assert not in_frame


def test_resolve_rgb_handles_hex_and_theme_fallback():
    assert kitty_spinner.resolve_rgb("#c4b5fd", "#000000") == (0xC4, 0xB5, 0xFD)
    assert kitty_spinner.resolve_rgb("nodim #006b80 bold", "#000000") == (0x00, 0x6B, 0x80)
    # ANSI names (`/syntax terminal`) use the theme-matched fallback hex for contrast.
    assert kitty_spinner.resolve_rgb("ansimagenta", "#7c3aed") == (0x7C, 0x3A, 0xED)
    assert kitty_spinner.resolve_rgb("fg:default dim", "#586575") == (0x58, 0x65, 0x75)


def test_wide_styles_stay_single_cell_on_tool_and_accent_slots():
    for wide_style in ("gyro", "helix"):
        ks = KittySpinners(status_style="helix", tool_style=wide_style, enabled=True)
        specs = ks._slot_specs(PALETTES["dark"], PALETTES["dark"])
        assert specs["tool"].cols == 1
        assert specs["accent"].cols == 1
        assert ks.fragment("tool") == ("class:kitty.spinner.tool", PLACEHOLDER)
        assert ks.fragment("accent") == ("class:kitty.spinner.accent", PLACEHOLDER)
        # Only `dual` has an inset task shape; other styles keep tasks on `accent`.
        assert "task" not in specs


def test_inset_spinners_leave_their_right_edge_clear():
    """An inset image's right cell is the gap before its row's text."""
    rgb = (0x88, 0xC0, 0xD0)
    for style in ("dual", "gyro", "helix"):
        spec = kitty_spinner.SlotSpec(style, 2, rgb, rgb, inset=True)
        style_spec = kitty_spinner.STYLES[style]
        width = 2 * kitty_spinner.CELL_W
        drawn: set[int] = set()
        for frame in range(0, style_spec.frames, 7):
            raw = kitty_spinner._render_frame(spec, frame)
            assert len(raw) == width * kitty_spinner.CELL_H * 4
            columns = {(i // 4) % width for i in range(3, len(raw), 4) if raw[i] > 8}
            # Every frame stays clear of the right third of the second cell,
            # and is never cut off by the box's left edge.
            assert max(columns) < width - 10, (style, frame)
            left_edge = max(raw[y * width * 4 + 3] for y in range(kitty_spinner.CELL_H))
            assert left_edge <= 8, (style, frame, left_edge)
            drawn |= columns
        # And it turns over the first cell, where other rows' 1-cell icons sit.
        assert min(drawn) < kitty_spinner.CELL_W // 2, style


def test_kitty_spinners_upload_sync_recolor_and_close():
    async def run():
        ks = KittySpinners(status_style="helix", tool_style="dual", enabled=True)
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

        # Slot lineup: 2x1 helix status, 2x1 gyro startup, inset 2x1 gyro aside,
        # 1x1 dual tool/accent, and inset 2x1 dual task.
        dark_specs = ks._slot_specs(PALETTES["dark"], PALETTES["dark"])
        assert (dark_specs["status"].style, dark_specs["status"].cols) == ("helix", 2)
        assert (dark_specs["startup"].style, dark_specs["startup"].cols) == ("gyro", 2)
        assert (dark_specs["aside"].style, dark_specs["aside"].cols) == ("gyro", 2)
        assert (dark_specs["tool"].style, dark_specs["tool"].cols) == ("dual", 1)
        assert (dark_specs["accent"].style, dark_specs["accent"].cols) == ("dual", 1)
        assert (dark_specs["task"].style, dark_specs["task"].cols) == ("dual", 2)
        # Sub-agents turn with an inset copy of the main agent's spinner.
        assert (dark_specs["agent"].style, dark_specs["agent"].cols) == ("helix", 2)
        assert all(dark_specs[slot].inset for slot in ("aside", "task", "agent"))
        assert not dark_specs["startup"].inset
        assert dark_specs["status"].is_light is False
        light_specs = ks._slot_specs(PALETTES["light"], PALETTES["light"])
        assert light_specs["status"].is_light is True

        # First sync prepares `startup` alone first so the boot border animates quickly.
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

        # Next before_render flushes `startup` and queues the remaining four slots.
        ks.sync(
            PALETTES["dark"],
            PALETTES["dark"],
            write_raw=write_raw,
            flush=flush,
            invalidate=invalidate,
        )
        assert ks.ready is True
        assert ks.active_for("startup") is True
        assert ks.active_for("status") is False
        assert ks.slot_cols("startup") == 2
        assert flushes == 1
        assert ks._task is not None
        await ks._task
        assert invalidates == 2

        # Third before_render flushes the remaining four slots without re-uploading `startup`.
        ks.sync(
            PALETTES["dark"],
            PALETTES["dark"],
            write_raw=write_raw,
            flush=flush,
            invalidate=invalidate,
        )
        assert ks.active_for("status") is True
        assert ks.active_for("tool") is True
        assert ks.active_for("accent") is True
        assert ks.active_for("aside") is True
        assert ks.active_for("startup") is True
        assert ks.slot_cols("status") == 2
        assert ks.slot_cols("startup") == 2
        assert ks.slot_cols("aside") == 2
        assert ks.slot_cols("task") == 2
        assert ks.slot_cols("tool") == 1
        assert ks.slot_cols("accent") == 1
        assert flushes == 2
        assert f"i={SLOTS['startup']}" not in writes[-1]
        # Combined payloads free old frames (d=I), upload each slot's cell width, and loop.
        combined = writes[0] + writes[1]
        for slot, img_id in SLOTS.items():
            cols = dark_specs[slot].cols
            assert f"a=d,d=I,i={img_id},q=2" in combined
            assert f"U=1,c={cols},r=1,i={img_id},q=2" in combined
            assert f"a=a,i={img_id},r=1," in combined

        # Same palette is a no-op.
        ks.sync(
            PALETTES["dark"],
            PALETTES["dark"],
            write_raw=write_raw,
            flush=flush,
            invalidate=invalidate,
        )
        assert len(writes) == 2

        # Switching to light palette prepares and flushes all five slots together.
        ks.sync(
            PALETTES["light"],
            PALETTES["light"],
            write_raw=write_raw,
            flush=flush,
            invalidate=invalidate,
        )
        assert ks._task is not None
        await ks._task
        assert invalidates == 3
        ks.sync(
            PALETTES["light"],
            PALETTES["light"],
            write_raw=write_raw,
            flush=flush,
            invalidate=invalidate,
        )
        assert len(writes) == 3
        for img_id in SLOTS.values():
            assert f"i={img_id}" in writes[-1]

        # Closing deletes every uploaded image with d=I and tolerates OSError.
        ks.close(write_raw, flush)
        assert ks.ready is False
        assert ks.enabled is False
        cleanup = writes[-1]
        for img_id in SLOTS.values():
            assert f"\x1b_Ga=d,d=I,i={img_id},q=2\x1b\\" in cleanup

    asyncio.run(run())


def test_fragments_and_layout_with_kitty_spinners_and_fallback():
    from rich.cells import cell_len

    ks = KittySpinners(status_style="helix", tool_style="dual", enabled=True)
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
    p2 = kitty_spinner.placeholder_text(2)
    assert cell_len(p2) == 2
    assert layout.status_spinner_mark() == ("class:kitty.spinner.status", p2)
    assert layout.tool_spinner_mark() == ("class:kitty.spinner.tool", PLACEHOLDER)
    assert layout.aside_spinner_mark() == ("class:kitty.spinner.aside", p2)
    assert layout.wait_spinner_mark() == ("class:kitty.spinner.accent", PLACEHOLDER)
    assert layout.plan_spinner_icon() == kitty_spinner.INSET_ICON == p2
    # Sub-agents are agents too: they turn with the main agent's spinner.
    assert layout.agent_spinner_icon() == p2
    # Without an inset task slot, the task falls back to the 1x1 accent one.
    ks._uploaded_slots = set(SLOTS) - {"task"}
    assert layout.plan_spinner_icon() == PLACEHOLDER
    ks._uploaded_slots = set(SLOTS)
    assert layout.startup_spinner_mark(0.0) == ("class:kitty.spinner.startup", p2)
    assert layout.refresh_interval() == 0.5

    # 1x1 status style emits a bare 1-cell placeholder.
    ks_narrow = KittySpinners(status_style="swept", tool_style="dual", enabled=True)
    ks_narrow.ready = True
    ks_narrow._uploaded_slots = set(SLOTS)
    assert ks_narrow.fragment("status") == ("class:kitty.spinner.status", PLACEHOLDER)
    assert ks_narrow.fragment("startup") == ("class:kitty.spinner.startup", p2)
    # A 1x1 main spinner gives sub-agents a 1x1 copy, which keeps its gap.
    narrow_agent = ks_narrow._slot_specs(PALETTES["dark"], PALETTES["dark"])["agent"]
    assert (narrow_agent.cols, narrow_agent.inset) == (1, False)

    # StatusLine preserves the dedicated Kitty style class on the 2-cell placeholder only
    # and keeps exact width math.
    frags = StatusLine("Thinking", elapsed=4.0).fragments(ks.fragment("status"), 40, rule=True)
    assert frags[0] == ("class:kitty.spinner.status", p2)
    assert frags[1] == ("class:activity.spinner", " ")
    assert sum(cell_len(text) for _, text in frags) == 40

    # Atomic truncation: narrow widths never crop a 2-cell placeholder in half or
    # attach an ellipsis inside a `class:kitty.spinner.*` fragment.
    narrow_status = StatusLine("Thinking").fragments(ks.fragment("status"), 1)
    assert not any(style.startswith("class:kitty.spinner.") for style, _ in narrow_status)
    exact_status = StatusLine("Thinking").fragments(ks.fragment("status"), 2)
    assert exact_status == [("class:kitty.spinner.status", p2)]

    # Startup border keeps the 2-cell Kitty fragment intact in both bordered and shaded modes.
    activity.startup_label = "Loading extensions"
    for frame_style, shaded in (("borders", False), ("shaded", True)):
        activity.frame_style = frame_style
        layout.frame_shaded = lambda s=shaded: s
        for cols in (60, 12, 6):
            layout.size = lambda c=cols: type("S", (), {"columns": c, "rows": 24})()
            border_frags = layout.startup_border().content.text()
            if not shaded:
                assert sum(cell_len(text) for _, text in border_frags) == cols
            else:
                assert sum(cell_len(text) for _, text in border_frags) <= cols
            for style, text in border_frags:
                if style.startswith("class:kitty.spinner."):
                    assert text == p2
            if cols >= 12:
                assert ("class:kitty.spinner.startup", p2) in border_frags

    # Plan row splits the placeholder icon onto `class:kitty.spinner.accent`,
    # even when truncated to narrow widths (`"\U0010eeee…"`).
    plan_frags = panel_fragments([("class:plan.in_progress", f"{PLACEHOLDER} Build feature")], 40)
    assert ("class:kitty.spinner.accent", PLACEHOLDER) in plan_frags
    narrow_frags = panel_fragments([("class:plan.in_progress", f"{PLACEHOLDER} Build feature")], 2)
    assert narrow_frags[0] == ("class:kitty.spinner.accent", PLACEHOLDER)

    # The inset task spinner is its own gap, so the text starts where a
    # 1-cell icon's would, and it shows whole or not at all.
    inset_row = plan_row({"status": "in_progress", "content": "Build feature"}, p2)
    assert inset_row[1] == f"{p2}Build feature"
    done_row = plan_row({"status": "completed", "content": "Build feature"}, p2)
    assert cell_len(inset_row[1]) == cell_len(done_row[1])
    assert panel_fragments([inset_row], 40) == [
        ("class:kitty.spinner.task", p2),
        ("class:plan.in_progress", "Build feature"),
    ]
    assert panel_fragments([inset_row], 2) == [("class:kitty.spinner.task", p2)]
    for cols in (1, 2, 3, 40):
        clipped = panel_fragments([inset_row], cols)
        assert sum(cell_len(text) for _, text in clipped) <= cols
        assert all(text == p2 for style, text in clipped if style.startswith("class:kitty"))

    # A sub-agent row's spinner gets its inset image, its head stays bold.
    agent_frags = panel_fragments(
        [("class:plan.agent,agent.hue.0", f"├─ {p2}reviewing · 5.5s · Thinking")], 60
    )
    assert agent_frags[:3] == [
        ("class:plan.tree", "├─ "),
        ("class:kitty.spinner.agent", p2),
        ("class:plan.agent,agent.hue.0 bold", "reviewing"),
    ]

    # The /btw row's inset spinner is its gap too, keeping `btw` in the
    # column where the tool rows' text starts.
    activity.asides.append(
        SimpleNamespace(running=True, question="why?", label="", activity="", elapsed=3.0)
    )
    aside = activity.aside_rows(layout.aside_spinner_mark(), 40)[0]
    assert aside[0] == ("class:kitty.spinner.aside", p2)
    assert aside[1][1].startswith("btw · why?")
    narrow_aside = activity.aside_rows(("class:kitty.spinner.aside", PLACEHOLDER), 40)[0]
    assert narrow_aside[1][1].startswith(" btw")
    activity.asides.clear()

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
