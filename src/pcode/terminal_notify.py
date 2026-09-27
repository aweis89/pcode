"""Escape sequences for the terminal emulator itself, not the screen: desktop
notifications (OSC 9) and tab progress (OSC 9;4).

Ghostty shows both by default (`desktop-notifications`, `progress-style`);
iTerm2 and WezTerm take OSC 9 too, and terminals that know neither ignore an
unknown OSC. tmux drops them unless wrapped for passthrough and the server has
`allow-passthrough on`.

Tab progress is only sent where it is known to be drawn (`progress_transport`):
iTerm2 before 3.6.6 reads any OSC 9 as a notification and would pop up "4;3"
on every turn, which is why pytest turned the same feature off by default.
"""

import asyncio
import os
import re
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from time import monotonic

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# OSC 9;4 states (ConEmu's numbering, which every terminal kept).
CLEAR, NORMAL, ERROR, INDETERMINATE, PAUSED = 0, 1, 2, 3, 4

# Ghostty drops a report nobody refreshes after about 15 seconds, and
# recommends re-sending well inside that; other terminals do not mind.
KEEPALIVE_SECONDS = 5.0
TICK_SECONDS = 1.0


def _passthrough(sequence: str) -> str:
    """tmux's DCS passthrough: the sequence reaches the outer terminal as is."""
    return "\x1bPtmux;" + sequence.replace("\x1b", "\x1b\x1b") + "\x1b\\"


def _outer(sequence: str) -> str:
    """Wrap for tmux, which otherwise swallows OSC sequences it does not know."""
    return _passthrough(sequence) if os.environ.get("TMUX") else sequence


def notification(text: str) -> str:
    # Control characters would end the sequence early; and a body starting
    # with "4;" would be read as a progress report, which "pcode" never is.
    body = _CONTROL.sub(" ", text).strip()[:200]
    return _outer(f"\x1b]9;{body}\x07")


def progress(state: int, value: int | None = None) -> str:
    """One OSC 9;4 report, unwrapped: `progress_transport` says how it travels."""
    return f"\x1b]9;4;{state}" + ("" if value is None else f";{value}") + "\x07"


# Which terminals draw OSC 9;4


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


def _number(text: str | None) -> int:
    return int(text) if text and text.isdigit() else 0


# kitty is left to `on`: before 0.38 it shows the report as a desktop
# notification, before 0.47 it draws nothing, and it exports no version.
_DRAWS = ("ghostty", "wezterm", "vscode", "mintty", "warpterminal")


def _iterm(name: str) -> bool:
    return name.lower() in ("iterm.app", "iterm2")


def _known(name: str, version: str = "") -> bool:
    """A terminal, by the name it gives itself, that draws the bar."""
    if name.lower() in _DRAWS:
        return True
    # Before 3.6.6 iTerm2 shows a notification instead: never guess its version.
    return _iterm(name) and _version(version) >= (3, 6, 6)


def _old_iterm(name: str, version: str) -> bool:
    return _iterm(name) and not _known(name, version)


def _progress_feature(features: str) -> bool:
    """iTerm2's TERM_FEATURES is a run of capitalised codes; "P" is progress."""
    return "P" in re.findall(r"[A-Z][a-z0-9]*", features.split()[0] if features else "")


def environment_supports(env: Mapping[str, str]) -> bool:
    """Whether the terminal pcode runs in directly draws the bar.

    The same checks as Cargo's (anstyle-progress), plus variables that survive
    where TERM_PROGRAM does not: ssh keeps TERM, and inside tmux both TERM and
    TERM_PROGRAM name tmux while the server's environment still names the
    terminal it was started from.
    """
    # A terminal that says it is an iTerm2 too old for the bar would show a
    # notification, whatever else a tmux server's stale environment claims.
    if _old_iterm(env.get("TERM_PROGRAM", ""), env.get("TERM_PROGRAM_VERSION", "")):
        return False
    if _old_iterm(env.get("LC_TERMINAL", ""), env.get("LC_TERMINAL_VERSION", "")):
        return False
    if _progress_feature(env.get("TERM_FEATURES", "")):
        return True
    if _known(env.get("TERM_PROGRAM", ""), env.get("TERM_PROGRAM_VERSION", "")):
        return True
    # iTerm2 sets these too, and ssh forwards LC_* by default.
    if _known(env.get("LC_TERMINAL", ""), env.get("LC_TERMINAL_VERSION", "")):
        return True
    if env.get("TERM") == "xterm-ghostty" or env.get("GHOSTTY_RESOURCES_DIR"):
        return True
    if env.get("WEZTERM_EXECUTABLE"):
        return True
    if env.get("WT_SESSION") or env.get("ConEmuANSI") == "ON":
        return True
    # VTE 0.79 (GNOME Terminal, Ptyxis) and Konsole 26.04 added it.
    return _number(env.get("VTE_VERSION")) >= 7900 or _number(env.get("KONSOLE_VERSION")) >= 260400


def _into_tmux(sequence: str) -> str:
    """Both ways through tmux, so neither needs asking about.

    The passthrough copy reaches the outer terminal when `allow-passthrough`
    is on, and is the only one that carries a keep-alive: tmux 3.7 forwards
    the raw copy itself, but only when the active pane's state changes. Older
    tmux drops the raw copy, and a terminal that gets both sees one state.
    """
    return _passthrough(sequence) + sequence


def progress_transport(
    mode: str, env: Mapping[str, str], *, tty: bool
) -> Callable[[str], str] | None:
    """How a report reaches the terminal (a wrapper), or None to send nothing.

    `mode` is the preference: "off", "on" (send whatever the terminal), or
    "auto" (only where the bar is known to be drawn).
    """
    if mode == "off" or not tty or env.get("TERM") == "dumb":
        return None
    if mode != "on" and not environment_supports(env):
        return None
    return _into_tmux if env.get("TMUX") else str


# Keeping the bar in step with the turn


class TabProgress:
    """Mirror the turn into the terminal's tab progress, and keep it alive.

    Blue (indeterminate) while a turn runs, filling as plan steps complete;
    orange (paused) while a failed provider request is retried; red (error)
    after a failed turn until a key is pressed here or the next turn starts.

    Sampled from the activity once a second rather than pushed from each hook,
    so a hosted session's mirrored state needs nothing extra, and written
    straight to the terminal: an invisible OSC has no reason to wait for, or
    cause, a repaint, and never lands inside a frame prompt_toolkit is still
    building. With no terminal (`fd` None) the hooks still work and nothing
    is sent.
    """

    def __init__(self, activity, fd: int | None, mode: str, env: Mapping[str, str] | None = None):
        self.activity = activity
        self.fd = fd
        self.mode = mode
        self.env = os.environ if env is None else env
        self.retrying = False
        # The last failure has been seen: its red bar is down.
        self.dismissed = False
        self.wrap: Callable[[str], str] | None = None
        self.sent: str | None = None
        self.sent_at = 0.0

    def report(self) -> tuple[int, int | None]:
        activity = self.activity
        if activity.prompt_state == "running" or activity.user_command:
            if self.retrying:
                return PAUSED, None
            plan = activity.plan
            done = sum(item.get("status") == "completed" for item in plan)
            # An empty bar reads as nothing happening; bounce until a step lands.
            if plan and done:
                return NORMAL, round(100 * done / len(plan))
            return INDETERMINATE, None
        if activity.prompt_state == "failed" and not self.dismissed:
            return ERROR, 100
        return CLEAR, None

    # The turn's hooks, from the terminal's view.

    def turn_started(self) -> None:
        self.retrying = False
        self.dismissed = False

    def turn_retry(self) -> None:
        self.retrying = True

    def turn_event(self) -> None:
        self.retrying = False

    def turn_ended(self) -> None:
        # A turn that failed mid-retry never sends the event that ends it.
        self.retrying = False

    def switched(self) -> None:
        """Another session is on screen: nothing seen or retried here is its."""
        self.retrying = False
        self.dismissed = False

    def key_pressed(self, _event=None) -> None:
        # Only once the red bar is up: typing ahead while a turn fails must
        # not dismiss it before it was ever shown.
        if self.sent == progress(ERROR, 100):
            self.dismissed = True

    def tick(self, now: float) -> None:
        sequence = progress(*self.report())
        idle = sequence == progress(CLEAR)
        if sequence != self.sent or (not idle and now - self.sent_at >= KEEPALIVE_SECONDS):
            self._write(sequence, now)

    def close(self) -> None:
        """Take the bar down on the way out; a dead pcode must not look busy."""
        if self.sent not in (None, progress(CLEAR)):
            self._write(progress(CLEAR), monotonic())

    async def run(self) -> None:
        tty = self.fd is not None and _isatty(self.fd)
        self.wrap = progress_transport(self.mode, self.env, tty=tty)
        if self.wrap is None:
            return
        try:
            while True:
                self.tick(monotonic())
                await asyncio.sleep(TICK_SECONDS)
        finally:
            self.close()

    @asynccontextmanager
    async def shown(self):
        """Keep the bar in step while the block runs, for a caller with no event loop of its own."""
        task = asyncio.create_task(self.run())
        try:
            yield self
        finally:
            task.cancel()
            # Now, not when the cancelled task next runs: the loop may be closing.
            self.close()

    def _write(self, sequence: str, now: float) -> None:
        if self.wrap is None:
            return
        data = self.wrap(sequence).encode()
        try:
            # All of it or nothing: a truncated OSC leaves the terminal inside
            # a string that swallows the next frame. The fd may have been made
            # non-blocking under us; prompt_toolkit flushes frames the same way.
            blocking = os.get_blocking(self.fd)
            os.set_blocking(self.fd, True)
            try:
                while data:
                    data = data[os.write(self.fd, data) :]
            finally:
                os.set_blocking(self.fd, blocking)
        except OSError:
            # The terminal is gone; the next tick tries again.
            return
        self.sent, self.sent_at = sequence, now


def _isatty(fd: int) -> bool:
    try:
        return os.isatty(fd)
    except OSError:
        return False


def terminal_fd(*streams) -> int | None:
    """The descriptor of the first stream that is a terminal, or None."""
    for stream in streams:
        try:
            fd = stream.fileno()
        except (AttributeError, OSError, ValueError):
            continue  # In memory (StringIO), or closed.
        if _isatty(fd):
            return fd
    return None


def send(output, sequence: str) -> None:
    """Queue on a prompt_toolkit output; it goes out with the next repaint.

    Never flushed here: scrollback handoffs build one atomic write across
    awaits, and flushing in the middle of one sends half a frame early (the
    tmux cursor tests caught exactly that). An invisible OSC riding along
    with a repaint changes nothing on screen.
    """
    try:
        output.write_raw(sequence)
    except Exception:  # noqa: BLE001 - a notification is never worth an error.
        pass
