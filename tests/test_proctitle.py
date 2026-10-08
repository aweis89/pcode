import os
import subprocess
import sys

import pytest

from pcode import proctitle

# Stands in for the console script: a shebang file named `pcode`, which the
# kernel runs as `python3 .../pcode ARGS`.
SCRIPT = """#!{python}
import os, subprocess, sys
from pcode.proctitle import rename
done = rename()
ps = subprocess.run(["ps", "-o", "args=", "-p", str(os.getpid())], capture_output=True, text=True)
print(done, ps.stdout.split(), sys.argv[1:])
"""


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS argv layout")
def test_console_script_shows_as_pcode_with_its_arguments(tmp_path):
    script = tmp_path / "pcode"
    script.write_text(SCRIPT.format(python=sys.executable))
    script.chmod(0o755)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    out = subprocess.run(
        [script, "--no-worktree", "a b"], capture_output=True, text=True, env=env, check=True
    ).stdout
    # Python's own sys.argv is a copy and keeps the real arguments.
    assert out.strip() == "True ['pcode', '--no-worktree', 'a', 'b'] ['--no-worktree', 'a b']"


def test_leaves_other_command_lines_alone():
    # Under pytest argv[0] is not the console script, so nothing is rewritten.
    assert proctitle.rename() is False
