"""Inbound envelope validation: malformed messages are rejected, never run."""

import json

import pytest

from nn_mcp_types import jsonrpc

from nn_mcp_stdio import Server, envelope
from nn_mcp_stdio.transport import MemoryTransport


async def run(server, inbound):
    transport = MemoryTransport([json.dumps(message) for message in inbound])
    await server.run(transport)
    return [json.loads(line) for line in transport.outbound]


def echo_server(calls):
    server = Server(name="demo", version="0.1.0")

    @server.request("echo")
    async def echo(params):
        calls.append(params)
        return {"echoed": params}

    return server


@pytest.mark.parametrize(
    "message, request_id",
    [
        ({"jsonrpc": "2.0", "id": 1, "method": ["echo"]}, 1),
        ({"jsonrpc": "1.0", "id": 2, "method": "echo"}, 2),
        ({"id": 3, "method": "echo"}, 3),
        ({"jsonrpc": "2.0", "id": 4, "method": "echo", "params": "oops"}, 4),
        ({"jsonrpc": "2.0", "id": 5, "method": "echo", "params": [1]}, 5),
        ({"jsonrpc": "2.0", "id": None, "method": "echo"}, None),
        ({"jsonrpc": "2.0", "id": {"a": 1}, "method": "echo"}, None),
        ({"jsonrpc": "2.0", "id": 1.5, "method": "echo"}, None),
        ({"jsonrpc": "2.0", "id": True, "method": "echo"}, None),
    ],
)
async def test_malformed_request_is_invalid_request(message, request_id):
    calls = []
    out = await run(echo_server(calls), [message])
    assert len(out) == 1
    assert out[0]["error"]["code"] == jsonrpc.INVALID_REQUEST
    assert out[0]["error"]["message"].startswith("invalid request: ")
    if request_id is None:
        assert "id" not in out[0]  # undeterminable id -> omitted, not null
    else:
        assert out[0]["id"] == request_id
    assert calls == []  # the handler is never reached


async def test_malformed_notification_is_invalid_request():
    # JSON-RPC answers an invalid message even when it carries no id.
    out = await run(echo_server([]), [{"jsonrpc": "2.0", "method": 7}])
    assert out[0]["error"]["code"] == jsonrpc.INVALID_REQUEST
    assert "id" not in out[0]


async def test_non_object_message_is_invalid_request():
    out = await run(echo_server([]), [[1, 2]])
    assert out[0]["error"]["code"] == jsonrpc.INVALID_REQUEST
    assert "id" not in out[0]


async def test_valid_request_still_answered():
    calls = []
    message = {"jsonrpc": "2.0", "id": 9, "method": "echo", "params": {"a": 1}}
    out = await run(echo_server(calls), [message])
    assert out == [{"jsonrpc": "2.0", "id": 9, "result": {"echoed": {"a": 1}}}]
    assert calls == [{"a": 1}]


async def test_inbound_responses_are_never_answered():
    malformed = {"jsonrpc": "2.0", "id": 1}
    valid = {"jsonrpc": "2.0", "id": 2, "result": {}}
    failed = {"jsonrpc": "2.0", "id": 3, "error": {"code": -1, "message": "x"}}
    assert await run(echo_server([]), [malformed, valid, failed]) == []


def test_parse_builds_the_envelope_dataclass():
    line = json.dumps({"jsonrpc": "2.0", "method": "note", "params": {}})
    assert envelope.parse(line) == jsonrpc.Notification(
        method="note", params={}, jsonrpc=jsonrpc.VERSION
    )


def test_parse_rejects_non_json():
    with pytest.raises(envelope.ParseError):
        envelope.parse("{not json")
