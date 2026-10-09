"""Keep extended keyboard reporting scoped to the renderer's terminal ownership."""

import os
import subprocess
from contextlib import contextmanager

from prompt_toolkit.output import Output
from prompt_toolkit.output.vt100 import Vt100_Output

from pcode.input_keys import configure_newline_keys

# Push exactly disambiguate-escape-codes (not releases or all-key reporting).
# Push/pop preserves the previous flags, including across nested applications.
KEYBOARD_PUSH = "\x1b[>1u"
KEYBOARD_POP = "\x1b[<u"
TMUX_ENABLE = "\x1b[>4;1m"
TMUX_RESTORE = "\x1b[>4;0m"


def _keyboard_sequences() -> tuple[str, str]:
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return KEYBOARD_PUSH, KEYBOARD_POP
    # tmux uses modifyOtherKeys, not Kitty's per-screen stack. Snapshot once
    # before writing anything: a later subprocess query can race buffered output
    # and mistake our own temporary mode for the inherited one.
    try:
        mode = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{pane_key_mode}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=0.5,
        ).stdout.strip()
    except OSError, subprocess.SubprocessError:
        return "", ""
    if mode == "VT10x":
        return TMUX_ENABLE, TMUX_RESTORE
    # Ext 1 / Ext 2 already distinguish modified Enter. Leave them alone; also
    # leave unknown modes alone rather than guessing what to restore on exit.
    return "", ""


@Output.register
class KeyboardProtocolOutput:
    """Pair keyboard mode with bracketed paste, including terminal handoffs.

    Renderer.reset leaves the alternate screen *before* disabling paste. Pop
    early in that case: the keyboard stacks belong to individual screens.
    Buffered writes share the renderer's flush, avoiding half-switched frames.
    """

    def __init__(self, output: Output):
        self.output = output
        self._active = False
        self._paste_enabled = False
        self._preserving = False
        self._enable, self._disable = _keyboard_sequences()
        configure_newline_keys()

    def __getattr__(self, name):
        return getattr(self.output, name)

    def enable_bracketed_paste(self) -> None:
        self.output.enable_bracketed_paste()
        self._paste_enabled = True
        if not self._active:
            self.output.write_raw(self._enable)
            self._active = True

    def _restore_keyboard(self) -> None:
        if self._active:
            self.output.write_raw(self._disable)
            self._active = False

    @contextmanager
    def preserve_keyboard(self):
        """Keep reporting through an atomic erase, but release if repaint fails."""
        previous = self._preserving
        self._preserving = True
        try:
            yield
        finally:
            self._preserving = previous
            if not self._preserving and not self._paste_enabled:
                self._restore_keyboard()
                self.output.flush()

    def disable_bracketed_paste(self) -> None:
        self._paste_enabled = False
        if not self._preserving:
            self._restore_keyboard()
        self.output.disable_bracketed_paste()

    def quit_alternate_screen(self) -> None:
        self._restore_keyboard()
        self.output.quit_alternate_screen()


def keyboard_output(output: Output) -> Output:
    """Negotiate only on VT100 terminals, never dummy or redirected outputs."""
    if isinstance(output, Vt100_Output) and output.stdout.isatty():
        return KeyboardProtocolOutput(output)
    return output
