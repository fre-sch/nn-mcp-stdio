"""Test support: a paced transport and the messages that drive it."""

import asyncio
import json

from nn_mcp_stdio.transport import Transport

PAUSE = 0.02  # long enough for spawned handlers to run between lines


class ScriptedTransport(Transport):
    """Paced inbound lines; with `hold_first_write`, the first outbound write
    blocks until inbound is exhausted, so later replies wait in the outbox."""

    def __init__(self, inbound, *, hold_first_write=False):
        self._inbound = list(inbound)
        self._released = asyncio.Event()
        self._hold_first_write = hold_first_write
        self.outbound = []

    async def read_line(self):
        await asyncio.sleep(PAUSE)
        if not self._inbound:
            self._released.set()
            return None
        return self._inbound.pop(0)

    async def write_line(self, line):
        if self._hold_first_write and not self.outbound:
            await self._released.wait()
        self.outbound.append(line)


async def run(server, inbound, **options):
    lines = [json.dumps(message) for message in inbound]
    transport = ScriptedTransport(lines, **options)
    await server.run(transport)
    return [json.loads(line) for line in transport.outbound]


def request(request_id, method):
    return {"jsonrpc": "2.0", "id": request_id, "method": method}


def cancel(request_id):
    return {
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": request_id},
    }
