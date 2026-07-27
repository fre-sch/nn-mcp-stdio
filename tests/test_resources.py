"""Resource handlers: registration, resources/list, resources/read, capability."""

import base64
import json

import pytest

from nn_mcp_stdio import Context, Server
from nn_mcp_stdio.transport import MemoryTransport
from nn_mcp_types import content
from nn_mcp_types import resources as resource_types
from nn_mcp_types.content import BlobResourceContents, TextResourceContents


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


# -- static resources: add_resource_from_literal ---------------------------


def definition(uri, **fields):
    return resource_types.Resource(uri=uri, name="static", **fields)


async def test_literal_str_is_listed_verbatim_and_read_as_text():
    server = Server(name="lib", version="0.1.0")
    server.add_resource_from_literal(
        definition(
            "file:///notes.md",
            title="Notes",
            mime_type="text/markdown",
        ),
        "# hello",
    )

    listed = await run(server, [request("resources/list", request_id=1)])
    (resource,) = listed[0]["result"]["resources"]
    assert resource["title"] == "Notes"  # full definition, verbatim
    assert resource["mimeType"] == "text/markdown"

    out = await run(server, [read("file:///notes.md")])
    (contents,) = out[0]["result"]["contents"]
    assert contents == {
        "uri": "file:///notes.md",
        "text": "# hello",
        "mimeType": "text/markdown",
    }


async def test_literal_bytes_is_read_as_base64_blob():
    server = Server(name="lib", version="0.1.0")
    server.add_resource_from_literal(
        definition("bin:///logo", mime_type="image/png"), b"\x89PNG\r\n"
    )

    out = await run(server, [read("bin:///logo")])
    (contents,) = out[0]["result"]["contents"]
    assert contents["blob"] == base64.b64encode(b"\x89PNG\r\n").decode("ascii")
    assert contents["mimeType"] == "image/png"


def test_literal_rejects_a_non_str_bytes_content():
    server = Server(name="lib", version="0.1.0")
    with pytest.raises(TypeError):
        server.add_resource_from_literal(definition("num:///bad"), 42)


# -- static resources: add_resource_from_path ------------------------------


async def test_path_without_a_classifier_is_a_blob(tmp_path):
    server = Server(name="lib", version="0.1.0")
    file = tmp_path / "data.bin"
    file.write_bytes(b"\x00\x01\x02")
    server.add_resource_from_path(
        definition("file:///data.bin", mime_type="application/octet-stream"),
        file,
    )

    out = await run(server, [read("file:///data.bin")])
    (contents,) = out[0]["result"]["contents"]
    assert contents["blob"] == base64.b64encode(b"\x00\x01\x02").decode("ascii")
    assert contents["mimeType"] == "application/octet-stream"


async def test_path_classifier_sets_type_and_mime(tmp_path):
    server = Server(name="lib", version="0.1.0")
    file = tmp_path / "config.json"
    file.write_text('{"debug": true}', encoding="utf-8")

    def describe(path, data):
        return "application/json", TextResourceContents

    server.add_resource_from_path(
        # definition advertises nothing; the classifier fills the read type
        definition("file:///config.json"),
        file,
        describe_contents=describe,
    )

    listed = await run(server, [request("resources/list", request_id=1)])
    (resource,) = listed[0]["result"]["resources"]
    assert (
        "mimeType" not in resource
    )  # the listing hint stays as defined (none)

    out = await run(server, [read("file:///config.json")])
    (contents,) = out[0]["result"]["contents"]
    assert contents == {
        "uri": "file:///config.json",
        "text": '{"debug": true}',
        "mimeType": "application/json",
    }


async def test_path_is_read_lazily_on_each_read(tmp_path):
    server = Server(name="lib", version="0.1.0")
    file = tmp_path / "live.txt"
    file.write_text("first", encoding="utf-8")
    server.add_resource_from_path(
        definition("file:///live.txt"),
        file,
        describe_contents=lambda path, data: (
            "text/plain",
            TextResourceContents,
        ),
    )

    first = await run(server, [read("file:///live.txt")])
    assert first[0]["result"]["contents"][0]["text"] == "first"

    file.write_text("second", encoding="utf-8")  # changes between reads
    second = await run(server, [read("file:///live.txt")])
    assert second[0]["result"]["contents"][0]["text"] == "second"


async def test_path_classifier_bad_block_type_is_a_type_error(tmp_path):
    server = Server(name="lib", version="0.1.0")
    file = tmp_path / "x.dat"
    file.write_bytes(b"x")
    server.add_resource_from_path(
        definition("file:///x.dat"),
        file,
        describe_contents=lambda path, data: ("text/plain", str),
    )

    built = server._resources["file:///x.dat"]
    with pytest.raises(TypeError):
        await built.read()
