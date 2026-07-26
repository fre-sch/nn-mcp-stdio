"""Structured tool output: opt-in via structured_content, no compat mirror."""

import dataclasses
import json

from nn_mcp_types import content, tools

from nn_mcp_stdio import Server
from nn_mcp_stdio.transport import MemoryTransport


@dataclasses.dataclass
class Stats:
    count: int
    ok: bool


def encode(obj):
    return json.dumps(obj)


async def run(server, inbound):
    transport = MemoryTransport(inbound)
    await server.run(transport)
    return [json.loads(line) for line in transport.outbound]


def call(name, arguments=None, request_id=2):
    return encode(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }
    )


def list_tools(request_id=1):
    return encode({"jsonrpc": "2.0", "id": request_id, "method": "tools/list"})


def stats_server():
    server = Server(name="demo", version="0.1.0")

    @server.tool(structured_content=True)
    async def stats() -> Stats:
        """Compute stats."""
        return Stats(count=3, ok=True)

    return server


async def test_structured_tool_advertises_output_schema():
    out = await run(stats_server(), [list_tools()])
    (tool,) = out[0]["result"]["tools"]
    assert tool["outputSchema"]["properties"] == {
        "count": {"type": "integer"},
        "ok": {"type": "boolean"},
    }
    assert tool["outputSchema"]["required"] == ["count", "ok"]


async def test_structured_tool_returns_structured_content_no_mirror():
    out = await run(stats_server(), [call("stats")])
    # structuredContent set; content empty -- no wasteful text mirror.
    assert out[0]["result"] == {
        "content": [],
        "structuredContent": {"count": 3, "ok": True},
    }


async def test_dict_return_is_structured_without_output_schema():
    server = Server(name="demo", version="0.1.0")

    @server.tool(structured_content=True)
    async def raw() -> dict:
        """Return a raw object."""
        return {"a": 1, "b": [2, 3]}

    out = await run(server, [list_tools(), call("raw", request_id=2)])
    (tool,) = out[0]["result"]["tools"]
    assert "outputSchema" not in tool  # dict return -> no declared schema
    assert out[1]["result"]["structuredContent"] == {"a": 1, "b": [2, 3]}


async def test_unstructured_tool_has_no_structured_content():
    server = Server(name="demo", version="0.1.0")

    @server.tool()
    async def hello() -> str:
        return "hi"

    out = await run(server, [list_tools(), call("hello", request_id=2)])
    (tool,) = out[0]["result"]["tools"]
    assert "outputSchema" not in tool
    assert out[1]["result"]["content"] == [{"type": "text", "text": "hi"}]
    assert "structuredContent" not in out[1]["result"]


async def test_structured_tool_can_return_call_tool_result_with_both():
    # escape hatch: a handler wanting the compat mirror builds it by hand.
    server = Server(name="demo", version="0.1.0")

    @server.tool(structured_content=True)
    async def both() -> Stats:
        return tools.CallToolResult(
            content=[content.TextContent(text='{"count": 1, "ok": true}')],
            structured_content={"count": 1, "ok": True},
        )

    out = await run(server, [call("both")])
    assert out[0]["result"]["structuredContent"] == {"count": 1, "ok": True}
    assert out[0]["result"]["content"][0]["text"] == '{"count": 1, "ok": true}'
