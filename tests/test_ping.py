"""ping: answered from the read loop, whatever the scheduler is doing."""

import asyncio

from nn_mcp_types import jsonrpc
from support import PAUSE, run

from nn_mcp_stdio import Server


def ping(request_id, params=None):
    message = {"jsonrpc": "2.0", "id": request_id, "method": "ping"}
    if params is not None:
        message["params"] = params
    return message


def initialize(request_id):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "client", "version": "1"},
        },
    }


def pong(request_id):
    return {"jsonrpc": "2.0", "id": request_id, "result": {}}


async def test_ping_before_and_after_initialize():
    server = Server(name="demo", version="0.1.0")
    out = await run(server, [ping(1), initialize(2), ping(3)])
    assert pong(1) in out and pong(3) in out


async def test_ping_with_params_is_answered_identically():
    server = Server(name="demo", version="0.1.0")
    out = await run(server, [ping(1, {"_meta": {"note": "x"}})])
    assert out == [pong(1)]


async def test_ping_is_answered_while_every_slot_is_busy():
    server = Server(name="demo", version="0.1.0", limit=1)
    order = []

    @server.request("slow")
    async def slow(params):
        await asyncio.sleep(10 * PAUSE)
        order.append("slow done")
        return {}

    busy = {"jsonrpc": "2.0", "id": 1, "method": "slow"}
    queued = {"jsonrpc": "2.0", "id": 2, "method": "slow"}
    out = await run(server, [busy, queued, ping(3)])
    # the ping is answered first, while both slow requests still hold or wait
    # for the single slot.
    assert out[0] == pong(3)
    assert [message["id"] for message in out[1:]] == [1, 2]


async def test_ping_reusing_an_id_in_flight_is_rejected():
    server = Server(name="demo", version="0.1.0")

    @server.request("slow")
    async def slow(params):
        await asyncio.sleep(5 * PAUSE)
        return {}

    out = await run(
        server, [{"jsonrpc": "2.0", "id": 1, "method": "slow"}, ping(1)]
    )
    assert out[0]["id"] == 1
    assert out[0]["error"]["code"] == jsonrpc.INVALID_REQUEST
    assert out[1] == {"jsonrpc": "2.0", "id": 1, "result": {}}


async def test_ping_cannot_be_replaced():
    server = Server(name="demo", version="0.1.0")

    @server.request("ping")
    async def custom(params):
        return {"custom": True}

    out = await run(server, [ping(1)])
    assert out == [pong(1)]
