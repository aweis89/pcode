"""Scrollback-native terminal UI preview."""

import time

# When this process first loaded pcode. Modules imported lazily after a merge
# or reinstall come from newer files than the ones loaded at startup.
STARTED = time.time()
