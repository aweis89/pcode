"""Relay one MCP stdio connection from `agy` to the pcode session that serves it.

`agy` starts this as the stdio MCP server pcode offers each Antigravity
session, with the session's Unix socket as the only argument. It is run by
path with `python -I` and imports nothing from pcode: `agy` gives an MCP
server about six seconds to answer, and importing pcode would spend most of it.
"""

import socket
import sys
import threading


def main() -> None:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.connect(sys.argv[1])

    def upstream() -> None:
        try:
            while chunk := sys.stdin.buffer.read1(65536):
                connection.sendall(chunk)
        except OSError:
            pass
        finally:
            # agy closed its end: let pcode see the end of the conversation.
            try:
                connection.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    threading.Thread(target=upstream, daemon=True).start()
    try:
        while chunk := connection.recv(65536):
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
    except OSError:
        pass


if __name__ == "__main__":
    main()
