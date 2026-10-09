"""Decoder-only tests; protocol negotiation is tested with the terminal lifecycle."""

import pytest
from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys

from pcode.input_keys import _MAX_SEQUENCE, _ORIGINAL_FEED, configure_newline_keys


@pytest.fixture
def parser():
    configure_newline_keys()
    events = []
    return Vt100Parser(events.append), events


def parse(parser, sequence):
    decoder, events = parser
    decoder.feed_and_flush(sequence)
    return [(event.key, event.data) for event in events]


@pytest.mark.parametrize(
    "sequence,key,data",
    [
        ("\x1b[13;5u", Keys.ControlF24, "\r"),
        ("\x1b[27;5;13~", Keys.ControlF24, "\r"),
        ("\x1b[13;2u", Keys.ControlJ, "\n"),
        ("\x1b[27;2;13~", Keys.ControlJ, "\n"),
        ("\x1b[106;5u", Keys.ControlJ, "\n"),
        ("\x1b[27;5;106~", Keys.ControlJ, "\n"),
        ("\x1b[27u", Keys.Escape, "\x1b"),
        ("\x1b[27;2u", Keys.ShiftEscape, "\x1b"),
        ("\x1b[9;2u", Keys.BackTab, "\t"),
        ("\x1b[127u", Keys.ControlH, "\x7f"),
        ("\x1b[8u", Keys.ControlH, "\x7f"),
        ("\x1b[13u", Keys.ControlM, "\r"),
        ("\x1b[97;5u", Keys.ControlA, "\x01"),
        ("\x1b[122;6u", Keys.ControlZ, "\x1a"),
        ("\x1b[47;5u", Keys.ControlUnderscore, "\x1f"),
        ("\x1b[27;5;47~", Keys.ControlUnderscore, "\x1f"),
        ("\x1b[32;5u", Keys.ControlAt, "\x00"),
        ("\x1b[64;5u", Keys.ControlAt, "\x00"),
        ("\x1b[91;5u", Keys.Escape, "\x1b"),
        ("\x1b[92;5u", Keys.ControlBackslash, "\x1c"),
        ("\x1b[93;5u", Keys.ControlSquareClose, "\x1d"),
        ("\x1b[94;5u", Keys.ControlCircumflex, "\x1e"),
        ("\x1b[95;5u", Keys.ControlUnderscore, "\x1f"),
        ("\x1b[63;5u", Keys.ControlH, "\x7f"),
        ("\x1b[97;197u", Keys.ControlA, "\x01"),
        ("\x1b[233u", "é", "é"),
        ("\x1b[128512u", "😀", "😀"),
        ("\x1b[1;5A", Keys.ControlUp, ""),
        ("\x1b[1;6B", Keys.ControlShiftDown, ""),
        ("\x1b[1;193D", Keys.Left, ""),
        ("\x1b[5;2~", Keys.ShiftPageUp, ""),
        ("\x1b[3;5~", Keys.ControlDelete, ""),
        ("\x1b[1;2P", Keys.F13, ""),
        ("\x1b[13;5~", Keys.ControlF3, ""),
        ("\x1b[57387u", Keys.F24, ""),
        ("\x1b[57400u", "1", "1"),
        ("\x1b[57409u", ".", "."),
        ("\x1b[57414;5u", Keys.ControlF24, "\r"),
        ("\x1b[57417;5u", Keys.ControlLeft, ""),
    ],
)
def test_protocol_keys(parser, sequence, key, data):
    assert parse(parser, sequence) == [(key, data)]


@pytest.mark.parametrize("sequence", ["\x1b[98;3u", "\x1b[27;3;98~"])
def test_alt_printable_has_actual_text_data(parser, sequence):
    assert parse(parser, sequence) == [(Keys.Escape, "\x1b"), ("b", "b")]


def test_alt_shift_and_control(parser):
    assert parse(parser, "\x1b[98;4u\x1b[98;7u\x1b[1;4A") == [
        (Keys.Escape, "\x1b"),
        ("B", "B"),
        (Keys.Escape, "\x1b"),
        (Keys.ControlB, "\x02"),
        (Keys.Escape, "\x1b"),
        (Keys.ShiftUp, ""),
    ]


@pytest.mark.parametrize("sequence", ["\x1b[13;5u", "\x1b[27;3;233~", "\x1b[1;197A"])
def test_all_fragment_boundaries(parser, sequence):
    decoder, events = parser
    expected = parse(parser, sequence)
    for boundary in range(1, len(sequence)):
        events.clear()
        decoder.reset()
        decoder.feed(sequence[:boundary])
        assert events == []
        decoder.feed_and_flush(sequence[boundary:])
        assert [(event.key, event.data) for event in events] == expected
    events.clear()
    for char in sequence:
        decoder.feed(char)
    assert [(event.key, event.data) for event in events] == expected


@pytest.mark.parametrize(
    "sequence",
    [
        "\x1b[57358u",
        "\x1b[57441u",
        "\x1b[57398u",
        "\x1b[57428u",
        "\x1b[97;9u",
        "\x1b[97;17u",
        "\x1b[97;33u",
        "\x1b[97;257u",
        "\x1b[97;0u",
        "\x1b[0u",
        "\x1b[1114112u",
        "\x1b[55296u",
        "\x1b[97;5:3u",
        "\x1b[97:65;2u",
        "\x1b[97;1;97u",
        "\x1b[999~",
        "\x1b[57427~",
        "\x1b[1;2E",
        "\x1b[57387;5u",  # Ctrl+F24 must not trigger the Ctrl+Enter binding.
        "\x1b[43;5u",  # No faithful Ctrl+plus representation.
        "\x1b[304;5u",  # Lowercasing can produce multiple codepoints.
        "\x1b[27;5~",
        "\x1b[27;5;;13~",
    ],
)
def test_unsupported_keys_are_consumed_without_text_or_escape(parser, sequence):
    assert parse(parser, sequence + "ok") == [("o", "o"), ("k", "k")]


@pytest.mark.parametrize(
    "sequence,expected",
    [
        (
            "hello\r\n\t\x7f",
            [(c, c) for c in "hello"]
            + [
                (Keys.ControlM, "\r"),
                (Keys.ControlJ, "\n"),
                (Keys.ControlI, "\t"),
                (Keys.ControlH, "\x7f"),
            ],
        ),
        ("\x1b", [(Keys.Escape, "\x1b")]),
        ("\x1bx", [(Keys.Escape, "\x1b"), ("x", "x")]),
        ("\x1b[A", [(Keys.Up, "\x1b[A")]),
        ("\x1bOP", [(Keys.F1, "\x1bOP")]),
        ("\x1b[1;5u", [(Keys.Control5, "\x1b[1;5u")]),
        ("\x1b[12;34R", [(Keys.CPRResponse, "\x1b[12;34R")]),
        ("\x1b[<0;10;20M", [(Keys.Vt100MouseEvent, "\x1b[<0;10;20M")]),
        ("\x1b[M !!", [(Keys.Vt100MouseEvent, "\x1b[M !!")]),
        ("\x1b[96;14;13M", [(Keys.Vt100MouseEvent, "\x1b[96;14;13M")]),
    ],
)
def test_legacy_input(parser, sequence, expected):
    assert parse(parser, sequence) == expected


@pytest.mark.parametrize(
    "sequence",
    [
        sequence
        for sequence in ANSI_SEQUENCES
        if sequence.startswith("\x1b[")
        and (
            (sequence.endswith("~") and ";" not in sequence)
            or sequence in {f"\x1b[1;9{direction}" for direction in "ABCD"}
        )
    ],
)
def test_preserved_legacy_aliases_match_original_parser(parser, sequence):
    expected = []
    original = Vt100Parser(expected.append)
    _ORIGINAL_FEED(original, sequence)
    original.flush()
    assert parse(parser, sequence) == [(event.key, event.data) for event in expected]


def test_bracketed_paste_bypasses_decoder_even_when_fragmented(parser):
    decoder, events = parser
    text = "a\x1b[13;5u\x1b[27u\n"
    sequence = "\x1b[200~" + text + "\x1b[201~\x1b[13;5u"
    for char in sequence:
        decoder.feed(char)
    decoder.flush()
    assert [(event.key, event.data) for event in events] == [
        (Keys.BracketedPaste, text),
        (Keys.ControlF24, "\r"),
    ]


def test_outer_legacy_alt_prefix_is_not_reordered(parser):
    assert parse(parser, "\x1b\x1b[98;3u") == [
        (Keys.Escape, "\x1b"),
        (Keys.Escape, "\x1b"),
        ("b", "b"),
    ]


def test_bounded_input_and_recovery(parser):
    decoder, events = parser
    decoder.feed("\x1b[" + "9" * (_MAX_SEQUENCE * 100) + "u")
    assert events == []
    decoder.feed_and_flush("x\x1b[13;5u")
    assert [(event.key, event.data) for event in events] == [("x", "x"), (Keys.ControlF24, "\r")]


def test_reset_and_flush_discard_partial_reports(parser):
    decoder, events = parser
    decoder.feed("\x1b[13;")
    decoder.reset()
    decoder.feed("x\x1b[97;")
    decoder.flush()
    decoder.feed_and_flush("y")
    assert [(event.key, event.data) for event in events] == [("x", "x"), ("y", "y")]


def test_input_created_before_configuration_gets_decoder(monkeypatch):
    monkeypatch.setattr(Vt100Parser, "feed", _ORIGINAL_FEED)
    events = []
    decoder = Vt100Parser(events.append)
    configure_newline_keys()
    decoder.feed_and_flush("\x1b[13;5u")
    assert [event.key for event in events] == [Keys.ControlF24]


def test_x10_mouse_payload_is_opaque(parser):
    decoder, events = parser
    report = "\x1b[M\x1b[A"
    for char in report:
        decoder.feed(char)
    decoder.feed_and_flush("\x1b[27u")
    assert [(event.key, event.data) for event in events] == [
        (Keys.Vt100MouseEvent, report),
        (Keys.Escape, "\x1b"),
    ]


def test_protocol_sequences_do_not_fill_global_prefix_cache(parser):
    from prompt_toolkit.input.vt100_parser import _IS_PREFIX_OF_LONGER_MATCH_CACHE

    decoder, events = parser
    before = set(_IS_PREFIX_OF_LONGER_MATCH_CACHE)
    for code in range(1000, 2000):
        decoder.feed(f"\x1b[{code};5u")
    assert set(_IS_PREFIX_OF_LONGER_MATCH_CACHE) == before
    assert events == []


def test_install_is_idempotent_and_parser_state_is_independent(parser):
    decoder, events = parser
    configure_newline_keys()
    other_events: list[KeyPress] = []
    other = Vt100Parser(other_events.append)
    decoder.feed("\x1b[13;")
    other.feed_and_flush("z")
    configure_newline_keys()
    decoder.feed_and_flush("5u")
    assert [event.key for event in events] == [Keys.ControlF24]
    assert [event.key for event in other_events] == ["z"]
