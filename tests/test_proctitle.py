import json
import os
import subprocess
import sys

import pytest

from pcode import proctitle

# Stands in for the console script: a shebang file named `pcode`, which the
# kernel runs as `python3 .../pcode ARGS`.
SCRIPT = """#!{python}
import json, sys, psutil
from pcode.proctitle import rename
done = rename()
me = psutil.Process()
print(json.dumps([done, me.cmdline(), me.environ().get("PROCTITLE_PROBE"), sys.argv]))
"""


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS argv layout")
def test_console_script_shows_as_itself_not_python(tmp_path):
    script = tmp_path / "pcode"
    script.write_text(SCRIPT.format(python=sys.executable))
    script.chmod(0o755)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path), "PROCTITLE_PROBE": "seen"}
    argv = [str(script), "--no-worktree", "", "a b"]
    out = subprocess.run(argv, capture_output=True, text=True, env=env, check=True).stdout
    done, cmdline, probe, python_argv = json.loads(out)
    assert done is True
    # What ps and iTerm2 read: the interpreter is gone, the rest kept.
    assert [arg for arg in cmdline if arg] == [str(script), "--no-worktree", "a b"]
    # The environment after the strings is still where readers look for it.
    assert probe == "seen"
    # Python's own sys.argv is a copy and is untouched.
    assert python_argv == argv


def test_leaves_other_command_lines_alone():
    # Under pytest argv[0] is not the console script, so nothing is rewritten.
    assert proctitle.rename() is False
