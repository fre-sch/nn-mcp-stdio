"""Offset pagination: the shared helper, and tools/list paged by it."""

import json

import pytest

from nn_mcp_types import jsonrpc

from nn_mcp_stdio import Server, errors, pagination
from nn_mcp_stdio.transport import MemoryTransport

ITEMS = ["a", "b", "c", "d", "e"]


def test_first_page_and_next_cursor():
    assert pagination.page(ITEMS, None, 2) == (["a", "b"], "2")


def test_middle_and_last_page():
    assert pagination.page(ITEMS, "2", 2) == (["c", "d"], "4")
    assert pagination.page(ITEMS, "4", 2) == (["e"], None)


def test_exact_fit_has_no_next_cursor():
    assert pagination.page(ITEMS[:4], "2", 2) == (["c", "d"], None)


def test_offset_past_the_end_is_an_empty_last_page():
    assert pagination.page(ITEMS, "9", 2) == ([], None)


def test_page_size_none_disables_pagination():
    assert pagination.page(ITEMS, None, None) == (ITEMS, None)


@pytest.mark.parametrize("cursor", ["-1", "1.5", "abc", "", " 2", "٣", 3])
def test_malformed_cursor_is_invalid_params(cursor):
    with pytest.raises(errors.RequestError) as raised:
        pagination.page(ITEMS, cursor, 2)
    assert raised.value.code == jsonrpc.INVALID_PARAMS


def test_page_size_must_be_positive():
    with pytest.raises(ValueError):
        Server(name="demo", version="0.1.0", page_size=0)


def server_with_tools(count, **options):
    server = Server(name="demo", version="0.1.0", **options)
    for index in range(count):

        async def handler() -> str:
            return "ok"

        server.tool(handler, name=f"tool_{index}")
    return server


async def list_tools(server, cursors):
    requests = []
    for request_id, cursor in enumerate(cursors):
        params = {} if cursor is None else {"cursor": cursor}
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/list",
            "params": params,
        }
        requests.append(json.dumps(request))
    transport = MemoryTransport(requests)
    await server.run(transport)
    return [json.loads(line) for line in transport.outbound]


async def test_tools_list_pages_through_every_tool_once():
    server = server_with_tools(5, page_size=2)
    first, second, third = await list_tools(server, [None, "2", "4"])
    assert first["result"]["nextCursor"] == "2"
    assert second["result"]["nextCursor"] == "4"
    assert "nextCursor" not in third["result"]
    names = [
        tool["name"]
        for page in (first, second, third)
        for tool in page["result"]["tools"]
    ]
    assert names == [f"tool_{index}" for index in range(5)]


async def test_fewer_tools_than_a_page_is_one_page():
    (only,) = await list_tools(server_with_tools(3), [None])
    assert len(only["result"]["tools"]) == 3
    assert "nextCursor" not in only["result"]


async def test_page_size_none_lists_everything_at_once():
    (only,) = await list_tools(server_with_tools(150, page_size=None), [None])
    assert len(only["result"]["tools"]) == 150
    assert "nextCursor" not in only["result"]


async def test_default_page_size_is_100():
    first, second = await list_tools(server_with_tools(150), [None, "100"])
    assert len(first["result"]["tools"]) == 100
    assert first["result"]["nextCursor"] == "100"
    assert len(second["result"]["tools"]) == 50


async def test_malformed_cursor_on_the_wire_is_invalid_params():
    (reply,) = await list_tools(server_with_tools(3), ["nope"])
    assert reply["error"]["code"] == jsonrpc.INVALID_PARAMS
