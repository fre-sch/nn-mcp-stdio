"""End-to-end server behaviour driven over an in-memory transport."""

import json

from nn_mcp_types import lifecycle

from nn_mcp_stdio import Server
from nn_mcp_stdio.transport import MemoryTransport


def encode(obj):
    return json.dumps(obj)


def initialize_request(request_id=1):
    return encode(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "client", "version": "1"},
            },
        }
    )


async def run(server, inbound):
    transport = MemoryTransport(inbound)
    await server.run(transport)
    return [json.loads(line) for line in transport.outbound]


async def test_initialize_handshake():
    server = Server(
        name="demo",
        version="0.1.0",
        capabilities=lifecycle.ServerCapabilities(
            tools=lifecycle.ToolsCapability()
        ),
    )
    initialized = encode(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    out = await run(server, [initialize_request(), initialized])

    # only the initialize request produces output; the notification does not.
    assert len(out) == 1
    resp = out[0]
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    assert resp["result"]["protocolVersion"] == "2025-11-25"
    assert resp["result"]["serverInfo"] == {"name": "demo", "version": "0.1.0"}
    assert "tools" in resp["result"]["capabilities"]


async def test_registered_request_handler():
    server = Server(name="demo", version="0.1.0")

    @server.request("ping")
    async def ping(params):
        return {"pong": params["value"]}

    out = await run(
        server,
        [
            encode(
                {
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "ping",
                    "params": {"value": 42},
                }
            )
        ],
    )
    assert out == [{"jsonrpc": "2.0", "id": 9, "result": {"pong": 42}}]


async def test_notification_handler_runs_without_reply():
    server = Server(name="demo", version="0.1.0")
    seen = []

    @server.notification("notifications/initialized")
    async def on_init(params):
        seen.append(params)

    out = await run(
        server,
        [encode({"jsonrpc": "2.0", "method": "notifications/initialized"})],
    )
    assert out == []  # notifications never get a reply
    assert seen == [None]


async def test_method_not_found():
    server = Server(name="demo", version="0.1.0")
    out = await run(
        server, [encode({"jsonrpc": "2.0", "id": 3, "method": "nope"})]
    )
    assert out[0]["id"] == 3
    assert out[0]["error"]["code"] == -32601


async def test_parse_error_has_no_id():
    server = Server(name="demo", version="0.1.0")
    out = await run(server, ["{not json"])
    assert out[0]["error"]["code"] == -32700
    assert "id" not in out[0]  # undeterminable id -> omitted, not null


async def test_handler_exception_becomes_internal_error():
    server = Server(name="demo", version="0.1.0")

    @server.request("boom")
    async def boom(params):
        raise RuntimeError("kaboom")

    out = await run(
        server, [encode({"jsonrpc": "2.0", "id": 5, "method": "boom"})]
    )
    assert out[0]["id"] == 5
    assert out[0]["error"]["code"] == -32603
