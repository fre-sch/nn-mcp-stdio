"""Tool registration, listing, and validated calls over an in-memory transport."""

import json

import pytest

from nn_mcp_types import content

from nn_mcp_stdio import Server
from nn_mcp_stdio.transport import MemoryTransport


def encode(obj):
    return json.dumps(obj)


async def run(server, inbound):
    transport = MemoryTransport(inbound)
    await server.run(transport)
    return [json.loads(line) for line in transport.outbound]


def call(name, arguments, request_id=2):
    return encode(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )


def greeter():
    server = Server(name="demo", version="0.1.0")

    @server.tool()
    async def greet(name: str) -> str:
        """Greet someone."""
        return f"hi {name}"

    return server


async def test_tools_list_derives_schema_and_description():
    out = await run(
        greeter(),
        [encode({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})],
    )
    (tool,) = out[0]["result"]["tools"]
    assert tool["name"] == "greet"
    assert tool["description"] == "Greet someone."
    assert tool["inputSchema"]["properties"] == {"name": {"type": "string"}}
    assert tool["inputSchema"]["required"] == ["name"]
    # strict by default -> the arguments object is closed.
    assert tool["inputSchema"]["additionalProperties"] is False


async def test_tools_call_invokes_handler_with_kwargs():
    out = await run(greeter(), [call("greet", {"name": "ada"})])
    assert out[0]["result"] == {"content": [{"type": "text", "text": "hi ada"}]}


async def test_tools_call_rejects_type_mismatch_without_coercion():
    # `name` is a string; 123 is not coerced -- it is an INVALID_PARAMS error.
    out = await run(greeter(), [call("greet", {"name": 123})])
    assert "result" not in out[0]
    assert out[0]["error"]["code"] == -32602


async def test_strict_arguments_reject_unknown_field():
    out = await run(greeter(), [call("greet", {"name": "x", "extra": 1})])
    assert out[0]["error"]["code"] == -32602


async def test_lenient_tool_allows_unknown_field():
    server = Server(name="demo", version="0.1.0")

    @server.tool(strict_arguments=False)
    async def greet(name: str) -> str:
        return f"hi {name}"

    out = await run(server, [call("greet", {"name": "x", "extra": 1})])
    assert out[0]["result"]["content"][0]["text"] == "hi x"


async def test_registering_a_tool_advertises_the_capability():
    out = await run(
        greeter(),
        [
            encode(
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
        ],
    )
    assert "tools" in out[0]["result"]["capabilities"]


async def test_tool_failure_is_a_result_not_a_protocol_error():
    server = Server(name="demo", version="0.1.0")

    @server.tool()
    async def boom(x: int) -> str:
        raise RuntimeError("kaboom")

    out = await run(server, [call("boom", {"x": 1})])
    # a tool's own failure surfaces as an error *result*, not a JSON-RPC error.
    assert "error" not in out[0]
    assert out[0]["result"]["isError"] is True
    assert out[0]["result"]["content"][0]["text"] == "kaboom"


async def test_unknown_tool_is_invalid_params():
    out = await run(greeter(), [call("nope", {})])
    assert out[0]["error"]["code"] == -32602


async def test_content_block_return_passes_through():
    server = Server(name="demo", version="0.1.0")

    @server.tool()
    async def emit() -> content.TextContent:
        return content.TextContent(text="direct")

    out = await run(server, [call("emit", {})])
    assert out[0]["result"]["content"] == [{"type": "text", "text": "direct"}]


def test_non_async_handler_is_rejected():
    server = Server(name="demo", version="0.1.0")
    with pytest.raises(TypeError, match="async def"):

        @server.tool()
        def sync_tool(a: int) -> str:
            return str(a)
