"""Show the process as `pcode` rather than `python3` in `ps` and terminal tabs.

The console script is a shebang file, so the kernel runs it as
`.../bin/python3 .../bin/pcode ARGS` and argv[0] is the interpreter. iTerm2's
default tab title appends the foreground job as `(python3)`, and it reads that
job from argv[0] in the process's own memory (sysctl KERN_PROCARGS2), as `ps`
does. Rewriting those bytes in place, the way setproctitle does, renames the
job without a re-exec or a compiled dependency. argv[0] becomes the script's
own path: iTerm2 shows only its basename, and `ps` still tells which install
(or worktree) a session runs.
"""

import ctypes
import os
import sys


def rename(name: str = "pcode") -> bool:
    """Drop the interpreter from the C argv, leaving the `name` script; True if done.

    Only for the console script on macOS: elsewhere (pytest, `python -m`) the
    C argv belongs to some other command line, and Linux terminals name tabs
    from the kernel's comm, which this does not touch.
    """
    if sys.platform != "darwin":
        return False
    orig = sys.orig_argv
    if len(orig) < 2 or orig[1] != sys.argv[0] or os.path.basename(sys.argv[0]) != name:
        return False
    try:
        return _overwrite(orig[1:])
    except OSError, AttributeError, ValueError:
        return False


def _overwrite(args: list[str]) -> bool:
    libc = ctypes.CDLL(None)
    libc._NSGetArgc.restype = ctypes.POINTER(ctypes.c_int)
    libc._NSGetArgv.restype = ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
    argc = libc._NSGetArgc().contents.value
    argv = libc._NSGetArgv().contents
    if argc != len(sys.orig_argv):
        return False
    # The strings must sit back to back, as exec lays them out, so one write
    # covers them all and nothing after the last one is touched.
    start = end = argv[0]
    for i in range(argc):
        if not argv[i] or argv[i] != end:
            return False
        end += len(ctypes.string_at(argv[i])) + 1
    # Readers find the environment that follows by counting argc NULs, so the
    # count must still end where the strings did: the spare bytes go in front,
    # where readers already skip the exec path's alignment NULs, and the
    # argument dropped becomes an empty one at the end, which they skip too.
    strings = b"".join(os.fsencode(arg) + b"\0" for arg in args)
    title = strings + b"\0" * (argc - len(args))
    if len(title) > end - start:
        return False
    ctypes.memmove(start, title.rjust(end - start, b"\0"), end - start)
    return True
