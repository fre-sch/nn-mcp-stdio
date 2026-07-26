"""stdio transport: newline-delimited framing over a byte stream.

`read_line`/`write_line` deal in whole lines of text -- one JSON message each;
JSON (de)serialisation is the server's concern. `StdioTransport` does blocking
readline/write in a thread executor, so it works cross-platform (including
Windows, where asyncio has no console-pipe transport).
"""

import asyncio
import sys


class Transport:
    """A line-delimited duplex channel. `read_line` returns None at EOF."""

    async def read_line(self):
        raise NotImplementedError

    async def write_line(self, line):
        raise NotImplementedError


class StdioTransport(Transport):
    """Real stdio: reads `stdin`, writes `stdout` (UTF-8). Logging is the
    caller's concern and must go to stderr, never here."""

    def __init__(self, stdin=None, stdout=None):
        self._stdin = stdin if stdin is not None else sys.stdin.buffer
        self._stdout = stdout if stdout is not None else sys.stdout.buffer

    async def read_line(self):
        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(None, self._stdin.readline)
        if not raw:
            return None  # EOF
        return raw.decode("utf-8").rstrip("\r\n")

    async def write_line(self, line):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._emit, line)

    def _emit(self, line):
        self._stdout.write((line + "\n").encode("utf-8"))
        self._stdout.flush()


class MemoryTransport(Transport):
    """In-process transport for tests and embedding: a fixed list of inbound
    lines, and captured outbound lines in `outbound`."""

    def __init__(self, inbound=()):
        self._inbound = list(inbound)
        self._pos = 0
        self.outbound = []

    async def read_line(self):
        if self._pos >= len(self._inbound):
            return None
        line = self._inbound[self._pos]
        self._pos += 1
        return line

    async def write_line(self, line):
        self.outbound.append(line)
