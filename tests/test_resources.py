"""Resource handlers: registration, resources/list, resources/read, capability."""

import base64
import json

import pytest

from nn_mcp_stdio import Context, Server
from nn_mcp_stdio.transport import MemoryTransport
from nn_mcp_types import content


def encode(obj):
    return json.dumps(obj)


async def run(server, inbound):
    transport = MemoryTransport(inbound)
    await server.run(transport)
    return [json.loads(line) for line in transport.outbound]


def request(method, params=None, request_id=2):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return encode(message)


def read(uri, request_id=2):
    return request("resources/read", {"uri": uri}, request_id=request_id)


def library():
    server = Server(name="lib", version="0.1.0")

    @server.resource(
        "file:///config.json",
        mime_type="application/json",
        description="The service configuration.",
    )
    async def config() -> str:
        return '{"debug": true}'

    return server


async def test_list_reports_the_registered_resource():
    out = await run(library(), [request("resources/list", request_id=1)])
    (resource,) = out[0]["result"]["resources"]
    assert resource["uri"] == "file:///config.json"
    assert resource["name"] == "config"  # defaults to the reader's name
    assert resource["mimeType"] == "application/json"
    assert resource["description"] == "The service configuration."


async def test_read_wraps_a_str_into_text_contents():
    out = await run(library(), [read("file:///config.json")])
    (contents,) = out[0]["result"]["contents"]
    assert contents == {
        "uri": "file:///config.json",
        "text": '{"debug": true}',
        "mimeType": "application/json",
    }


async def test_read_wraps_bytes_into_a_base64_blob():
    server = Server(name="lib", version="0.1.0")

    @server.resource("bin:///logo", mime_type="image/png")
    async def logo() -> bytes:
        return b"\x89PNG\r\n"

    out = await run(server, [read("bin:///logo")])
    (contents,) = out[0]["result"]["contents"]
    assert contents["blob"] == base64.b64encode(b"\x89PNG\r\n").decode("ascii")
    assert contents["uri"] == "bin:///logo"
    assert contents["mimeType"] == "image/png"


async def test_read_passes_through_explicit_contents():
    server = Server(name="lib", version="0.1.0")

    @server.resource("doc:///two")
    async def two() -> list:
        return [
            content.TextResourceContents(uri="doc:///two#a", text="a"),
            content.TextResourceContents(uri="doc:///two#b", text="b"),
        ]

    out = await run(server, [read("doc:///two")])
    uris = [part["uri"] for part in out[0]["result"]["contents"]]
    assert uris == ["doc:///two#a", "doc:///two#b"]


async def test_read_unknown_uri_is_resource_not_found():
    out = await run(library(), [read("file:///missing")])
    assert out[0]["error"]["code"] == -32002
    assert "file:///missing" in out[0]["error"]["message"]


async def test_reader_receives_context():
    server = Server(name="lib", version="0.1.0")

    @server.resource("who:///am/i")
    async def who(ctx: Context) -> str:
        return str(ctx.request_id)

    out = await run(server, [read("who:///am/i", request_id=7)])
    (contents,) = out[0]["result"]["contents"]
    assert contents["text"] == "7"


async def test_initialize_advertises_resources_capability():
    initialize = encode(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "c", "version": "1"},
            },
        }
    )
    out = await run(library(), [initialize])
    assert "resources" in out[0]["result"]["capabilities"]


async def test_no_resources_no_capability():
    initialize = encode(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "c", "version": "1"},
            },
        }
    )
    out = await run(Server(name="bare", version="0.1.0"), [initialize])
    assert "resources" not in out[0]["result"]["capabilities"]


def test_non_async_reader_is_rejected():
    server = Server(name="lib", version="0.1.0")
    with pytest.raises(TypeError):

        @server.resource("sync:///no")
        def config():
            return "x"


async def test_bad_return_is_a_type_error():
    server = Server(name="lib", version="0.1.0")

    @server.resource("num:///bad")
    async def bad() -> int:
        return 42

    built = server._resources["num:///bad"]
    with pytest.raises(TypeError):
        await built.read()
