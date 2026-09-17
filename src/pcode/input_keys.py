"""Normalize explicit terminal newline chords before vi sees Escape.

prompt_toolkit 3.0.53 does not decode CSI-u Ctrl+J/Shift+Enter and maps
xterm's modified Shift+Enter to ControlM (submit). Keep this narrow: we do
not enable the Kitty keyboard protocol or claim general CSI-u support.
"""

from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.input.vt100_parser import _IS_PREFIX_OF_LONGER_MATCH_CACHE
from prompt_toolkit.keys import Keys

_NEWLINE_SEQUENCES = (
    "\x1b[106;5u",  # CSI-u Ctrl+J
    "\x1b[27;5;106~",  # xterm modifyOtherKeys Ctrl+J
    "\x1b[13;2u",  # CSI-u Shift+Enter
    "\x1b[27;2;13~",  # xterm modifyOtherKeys Shift+Enter
)


def configure_newline_keys() -> None:
    """Register process-wide VT100 aliases, including for pipe-input tests."""
    if any(ANSI_SEQUENCES.get(sequence) != Keys.ControlJ for sequence in _NEWLINE_SEQUENCES):
        ANSI_SEQUENCES.update(dict.fromkeys(_NEWLINE_SEQUENCES, Keys.ControlJ))
        # The parser caches prefixes globally; an earlier prompt may already
        # have classified these sequences as unknown. Recompute after updates.
        _IS_PREFIX_OF_LONGER_MATCH_CACHE.clear()
