"""Small real-PTY smoke tests, not a substitute for trying your terminal/tmux."""

import os
import re
import sys
from io import StringIO

import pytest

pexpect = pytest.importorskip("pexpect", reason="PTY smoke tests require a Unix terminal")
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Unix PTY required")


@pytest.mark.parametrize("columns", [40, 100])
def test_terminal_completion_resize_interrupt_and_exit(columns):
    log = StringIO()
    child = pexpect.spawn(
        sys.executable,
        ["-m", "pcode.app"],
        env={**os.environ, "TERM": "xterm-256color", "PROMPT_TOOLKIT_NO_CPR": "1"},
        dimensions=(30, columns),
        encoding="utf-8",
        timeout=10,
    )
    child.logfile_read = log
    try:
        child.expect_exact("\x1b[?25h")  # A completed prompt repaint; PTYs do not answer CPR.
        child.send("/")
        child.expect_exact("/help")  # Menu appears without pressing Tab.
        child.sendcontrol("c")
        child.expect_exact("Input discarded.")
        child.expect_exact("\x1b[?25h")  # A completed prompt repaint; PTYs do not answer CPR.
        child.send("/demo\r")
        child.expect_exact("No files were")
        child.expect_exact("\x1b[?25h")  # A completed prompt repaint; PTYs do not answer CPR.
        child.setwinsize(24, 32)
        child.send("/theme light\r")
        child.expect_exact("Theme: light.")
        child.expect_exact("\x1b[?25h")  # A completed prompt repaint; PTYs do not answer CPR.
        child.sendcontrol("d")
        child.expect(pexpect.EOF)
        child.close()
        assert child.exitstatus == 0
        output = log.getvalue()
        assert "Session not saved;" in output
        assert "Goodbye." not in output
        assert "\x1b[?1049h" not in output  # Output stays in normal scrollback.
        assert "\x1b[?1049l" not in output
        assert "\x1b[?1047h" not in output
        assert "\x1b[3J" not in output  # No scrollback erasure.
        assert not re.search(r"\x1b\[\d*;\d*r", output)  # No scroll region.
        assert "Traceback" not in output
    finally:
        if child.isalive():
            child.terminate(force=True)
