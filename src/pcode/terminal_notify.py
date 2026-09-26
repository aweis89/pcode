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
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
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


def _known(name: str, version: str = "") -> bool:
    """A terminal, by the name it gives itself, that draws the bar."""
    name = name.lower()
    if name in ("ghostty", "wezterm", "kitty", "vscode", "mintty", "warpterminal"):
        return True
    # Before 3.6.6 iTerm2 shows a notification instead: never guess its version.
    return name in ("iterm.app", "iterm2") and _version(version) >= (3, 6, 6)


def _progress_feature(features: str) -> bool:
    """iTerm2's TERM_FEATURES is a run of capitalised codes; "P" is progress."""
    return "P" in re.findall(r"[A-Z][a-z0-9]*", features.split()[0] if features else "")


def environment_supports(env: Mapping[str, str]) -> bool:
    """Whether the terminal pcode runs in directly draws the bar.

    The same checks as Cargo's (anstyle-progress), plus the TERM values that
    survive ssh, where TERM_PROGRAM does not.
    """
    if _progress_feature(env.get("TERM_FEATURES", "")):
        return True
    if _known(env.get("TERM_PROGRAM", ""), env.get("TERM_PROGRAM_VERSION", "")):
        return True
    # iTerm2 sets these too, and ssh forwards LC_* by default.
    if _known(env.get("LC_TERMINAL", ""), env.get("LC_TERMINAL_VERSION", "")):
        return True
    if env.get("TERM") in ("xterm-ghostty", "xterm-kitty") or env.get("KITTY_WINDOW_ID"):
        return True
    if env.get("WT_SESSION") or env.get("ConEmuANSI") == "ON":
        return True
    # VTE 0.79 (GNOME Terminal, Ptyxis) and Konsole 26.04 added it.
    return _number(env.get("VTE_VERSION")) >= 7900 or _number(env.get("KONSOLE_VERSION")) >= 260400


@dataclass(frozen=True)
class Tmux:
    """What the tmux server says about itself and the terminal attached to it."""

    version: tuple[int, ...]
    passthrough: bool
    # The attached terminal: its XTVERSION answer ("ghostty 1.2.0"), TERM, and
    # the features tmux enabled for it ("progressbar" since tmux 3.7).
    termtype: str = ""
    termname: str = ""
    features: tuple[str, ...] = ()

    @property
    def outer_supports(self) -> bool:
        if "progressbar" in self.features or self.termname in ("xterm-ghostty", "xterm-kitty"):
            return True
        name, _, version = self.termtype.partition(" ")
        return _known(name.split("(")[0], version or name)

    @property
    def forwards(self) -> bool:
        """tmux 3.7 parses OSC 9;4 itself and passes the active pane's on."""
        return self.version >= (3, 7) and "progressbar" in self.features


_TMUX_FORMAT = (
    "#{version}\t#{allow-passthrough}\t#{client_termtype}\t#{client_termname}\t"
    "#{client_termfeatures}"
)


def query_tmux(env: Mapping[str, str]) -> Tmux | None:
    """Ask the tmux server holding this pane; None when it cannot be asked."""
    pane = env.get("TMUX_PANE")
    try:
        answer = subprocess.run(
            ["tmux", "display-message", "-p", *(["-t", pane] if pane else []), _TMUX_FORMAT],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    fields = answer.rstrip("\n").split("\t")
    if len(fields) != 5:
        return None
    version, passthrough, termtype, termname, features = fields
    return Tmux(
        version=_version(version),
        passthrough=passthrough in ("on", "all"),
        termtype=termtype,
        termname=termname,
        features=tuple(feature for feature in features.split(",") if feature),
    )


def progress_transport(
    mode: str,
    env: Mapping[str, str],
    *,
    tty: bool,
    tmux: Callable[[Mapping[str, str]], Tmux | None] = query_tmux,
) -> Callable[[str], str] | None:
    """How a report reaches the terminal (a wrapper), or None to send nothing.

    `mode` is the preference: "off", "on" (send whatever the terminal), or
    "auto" (only where the bar is known to be drawn). Inside tmux passthrough
    wins when allowed, because tmux's own forwarding sends a report only when
    it changes, so a keep-alive never reaches the outer terminal.
    """
    if mode == "off" or not tty or env.get("TERM") == "dumb":
        return None
    forced = mode == "on"
    if not env.get("TMUX"):
        return str if forced or environment_supports(env) else None
    info = tmux(env)
    if info is None:
        # No answer from tmux: go on what the server inherited, as before.
        return _passthrough if forced or environment_supports(env) else None
    if not (forced or info.outer_supports):
        return None
    if info.passthrough:
        return _passthrough
    return str if info.forwards else None


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
    building.
    """

    def __init__(self, activity, fd: int, mode: str, env: Mapping[str, str] | None = None):
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

    def key_pressed(self, _event=None) -> None:
        if self.activity.prompt_state == "failed":
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
        tty = _isatty(self.fd)
        # Asking tmux spawns a process: keep it off the event loop.
        self.wrap = await asyncio.to_thread(progress_transport, self.mode, self.env, tty=tty)
        if self.wrap is None:
            return
        try:
            while True:
                self.tick(monotonic())
                await asyncio.sleep(TICK_SECONDS)
        finally:
            self.close()

    def _write(self, sequence: str, now: float) -> None:
        if self.wrap is None:
            return
        try:
            os.write(self.fd, self.wrap(sequence).encode())
        except OSError:
            # A non-blocking tty that is full, or one already gone: the next
            # tick tries again.
            return
        self.sent, self.sent_at = sequence, now


def _isatty(fd: int) -> bool:
    try:
        return os.isatty(fd)
    except OSError:
        return False


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
