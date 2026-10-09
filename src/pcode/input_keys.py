"""Decode Kitty flag-1 and xterm modified keys before vi sees Escape.

Mode negotiation belongs to the terminal lifecycle, not this module. The small
bounded interceptor leaves legacy input (including paste, CPR and mouse) to the
installed prompt_toolkit parser. Unsupported protocol keys are consumed, never
inserted as the literal bytes of their escape sequence.
"""

from collections.abc import Generator

from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.input.vt100_parser import Vt100Parser, _Flush
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys

_MAX_SEQUENCE = 128
_ORIGINAL_FEED = Vt100Parser.feed
_NAVIGATION = {
    "A": Keys.Up,
    "B": Keys.Down,
    "C": Keys.Right,
    "D": Keys.Left,
    "F": Keys.End,
    "H": Keys.Home,
    "P": Keys.F1,
    "Q": Keys.F2,
    "S": Keys.F4,
}
_TILDE_KEYS = {
    1: Keys.Home,
    2: Keys.Insert,
    3: Keys.Delete,
    4: Keys.End,
    5: Keys.PageUp,
    6: Keys.PageDown,
    7: Keys.Home,
    8: Keys.End,
    **{
        code: Keys[f"F{number}"]
        for number, code in enumerate((11, 12, 13, 14, 15, 17, 18, 19, 20, 21, 23, 24), 1)
    },
}
_KEYPAD = {
    **{57399 + i: str(i) for i in range(10)},
    **dict(zip(range(57409, 57417), (".", "/", "*", "-", "+", "\r", "=", ","))),
    **dict(
        zip(
            range(57417, 57427),
            (
                Keys.Left,
                Keys.Right,
                Keys.Up,
                Keys.Down,
                Keys.PageUp,
                Keys.PageDown,
                Keys.Home,
                Keys.End,
                Keys.Insert,
                Keys.Delete,
            ),
        )
    ),
}


def _modified_key(key: str | Keys, modifiers: int) -> list[KeyPress]:
    """Translate only modifiers that prompt_toolkit can represent faithfully."""
    if not 1 <= modifiers <= 256:
        return []
    bits = (modifiers - 1) & ~192  # Caps/Num Lock are state, not shortcut modifiers.
    if bits & ~7:  # Super, Hyper and Meta have no prompt_toolkit equivalent.
        return []
    shift, alt, control = bool(bits & 1), bool(bits & 2), bool(bits & 4)
    if isinstance(key, Keys):
        name = key.name
        if name.startswith("F"):
            number = int(name[1:]) + (12 if shift else 0)
            if number > 24:
                return []
            key = Keys[f"{'Control' if control else ''}F{number}"]
            if key == Keys.ControlF24:  # Reserved for Ctrl+Enter's send binding.
                return []
        elif shift or control:
            name = ("Control" if control else "") + ("Shift" if shift else "") + name
            if name not in Keys.__members__:
                return []
            key = Keys[name]
        data = ""
    elif key == "\r":
        key = Keys.ControlF24 if control else Keys.ControlJ if shift else Keys.ControlM
        data = "\n" if key == Keys.ControlJ else "\r"
    elif key == "\t":
        key, data = (Keys.BackTab if shift else Keys.ControlI), "\t"
    elif key in ("\x7f", "\b"):
        key, data = Keys.ControlH, "\x7f"
    elif key == "\x1b":
        key, data = (Keys.ShiftEscape if shift else Keys.Escape), "\x1b"
    elif control:
        char = key.lower()
        if len(char) == 1 and "a" <= char <= "z":
            key, data = Keys[f"Control{char.upper()}"], chr(ord(char) - 96)
        elif char in "0123456789":
            key, data = Keys[f"Control{'Shift' if shift else ''}{char}"], ""
        else:
            code = next(
                (
                    code
                    for chars, code in (
                        (" @`", 0),
                        ("[{", 27),
                        ("\\|", 28),
                        ("]}", 29),
                        ("^~", 30),
                        ("_/", 31),
                        ("?", 127),
                    )
                    if char in chars
                ),
                None,
            )
            if code is None:
                return []
            data = chr(code)
            key = ANSI_SEQUENCES[data]
            assert isinstance(key, Keys)
    else:
        # Flag 1 reports text directly. For encoded Alt+letters, recover the
        # printable payload rather than handing an escape sequence to insertion.
        data = key.upper() if shift and key.isascii() and key.isalpha() else key
        key = data
    result = [KeyPress(key, data)]
    return [KeyPress(Keys.Escape, "\x1b"), *result] if alt else result


def _decode(sequence: str) -> list[KeyPress] | None:
    """None delegates to VT100; an empty list consumes an unsupported key."""
    body, final = sequence[2:-1], sequence[-1]
    fields = body.split(";")
    if final not in "u~ABCDEFHPQS":
        return None
    # Preserve known legacy aliases rather than reinterpreting their parameters:
    # Windows BackTab/scroll and F13-F20 use bare tilde reports, while iTerm's
    # legacy Meta arrows use modifier 9 (which overlaps Kitty's Super bit).
    # In that overlap, the existing terminal shortcut takes precedence.
    if sequence in ANSI_SEQUENCES and (
        (final == "~" and ";" not in body) or (final in "ABCD" and body == "1;9")
    ):
        return None
    # CPR ends in R, and mouse reports in M/m: neither enters this decoder.
    if final == "~" and body in ("200", "201"):
        return None
    if final not in "u~" and ";" not in body:
        return None
    # Preserve prompt_toolkit's old modified-keypad aliases, which overlap
    # CSI-u's (invalid for flag 1) codepoint 1.
    if final == "u" and fields[0] == "1" and sequence in ANSI_SEQUENCES:
        key = ANSI_SEQUENCES[sequence]
        keys = key if isinstance(key, tuple) else (key,)
        return [KeyPress(k, sequence if i == 0 else "") for i, k in enumerate(keys)]
    # Alternate key codes, event types and text payloads are not negotiated by
    # flag 1. Consume those extensions rather than treating releases as presses.
    if ":" in body or any(field and not field.isascii() for field in fields):
        return []
    try:
        if final == "~" and fields[0] == "27":
            if len(fields) != 3:
                return []
            code, modifiers = int(fields[2]), int(fields[1])
            final = "u"
        else:
            if len(fields) > 2:
                return []
            code = int(fields[0] or "1")
            modifiers = int(fields[1] or "1") if len(fields) == 2 else 1
    except ValueError:
        return []
    if final == "u":
        if code in _KEYPAD:
            key = _KEYPAD[code]
        elif 57376 <= code <= 57387:
            key = Keys[f"F{code - 57363}"]
        elif code in (8, 9, 13, 27, 127) or (
            32 <= code <= 0x10FFFF and not 0xD800 <= code <= 0xF8FF
        ):
            key = chr(code)
            if not key.isprintable() and code not in (8, 9, 13, 27, 127):
                return []
        else:
            return []
    elif final == "~":
        key = _TILDE_KEYS.get(code)
    else:
        key = _NAVIGATION.get(final) if code == 1 else None
    return _modified_key(key, modifiers) if key is not None else []


def _keyboard_parser(self: Vt100Parser) -> Generator[None, str | _Flush, None]:
    legacy = self._input_parser
    pending = ""
    overflow = False
    mouse_remaining = 0
    while True:
        char = yield
        if isinstance(char, _Flush):
            # A timed-out numeric CSI is not printable text. Keep bare Escape
            # and Escape+[ working as before, but discard incomplete reports.
            if pending in ("\x1b", "\x1b["):
                for part in pending:
                    legacy.send(part)
            pending, overflow, mouse_remaining = "", False, 0
            legacy.send(char)
            continue
        if mouse_remaining:
            # X10 mouse payload is three opaque characters, not keyboard input.
            legacy.send(char)
            mouse_remaining -= 1
            continue
        if overflow:
            if "@" <= char <= "~":
                overflow = False
            elif char == "\x1b":
                overflow, pending = False, char
            continue
        if pending == "\x1b" and char != "[":
            legacy.send(pending)
            pending = ""
        if not pending:
            if char == "\x1b":
                pending = char
            else:
                legacy.send(char)
            continue
        pending += char
        if pending == "\x1b[":
            continue
        if char in "0123456789;:":
            if len(pending) >= _MAX_SEQUENCE:
                pending, overflow = "", True
            continue
        decoded = _decode(pending)
        if decoded is None:
            if pending == "\x1b[M":
                mouse_remaining = 3
            for part in pending:
                legacy.send(part)
        else:
            # An outer legacy Alt prefix (Escape followed by CSI-u) must be
            # delivered before the decoded key, not on the next timeout.
            legacy.send(_Flush())
            for key in decoded:
                self.feed_key_callback(key)
        pending = ""


def _feed_keyboard(self: Vt100Parser, data: str) -> None:
    # Inputs can be constructed before configure_newline_keys(), and reset()
    # recreates their coroutine. Wrap lazily so both paths get the decoder.
    if self._input_parser.gi_code is not _keyboard_parser.__code__:
        parser = _keyboard_parser(self)
        next(parser)
        self._input_parser = parser
    _ORIGINAL_FEED(self, data)


def configure_newline_keys() -> None:
    """Install once for all VT100 inputs, including already-created pipe inputs.

    Only feed() is patched; its original implementation still owns paste. The
    interceptor's bounded state is per parser and reset() discards it normally.
    No dynamic strings are added to prompt_toolkit's global prefix cache.
    """
    Vt100Parser.feed = _feed_keyboard
