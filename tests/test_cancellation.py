"""Inbound notifications/cancelled: a cancelled request is never answered."""

import asyncio

from nn_mcp_types import jsonrpc
from support import PAUSE, cancel, request, run

from nn_mcp_stdio import Server


def demo_server(events):
    server = Server(name="demo", version="0.1.0", limit=1)

    @server.request("quick")
    async def quick(params):
        events.append("quick ran")
        return {}

    @server.request("slow")
    async def slow(params):
        try:
            await asyncio.sleep(10 * PAUSE)
        except asyncio.CancelledError:
            events.append("slow cancelled")
            raise
        return {}

    @server.request("stubborn")
    async def stubborn(params):
        try:
            await asyncio.sleep(10 * PAUSE)
        except asyncio.CancelledError:
            events.append("stubborn swallowed")
        return {"answered": "anyway"}

    return server


async def test_cancelling_a_running_request_sends_nothing():
    events = []
    out = await run(demo_server(events), [request(1, "slow"), cancel(1)])
    assert out == []
    assert events == ["slow cancelled"]


async def test_cancelling_a_queued_request_never_runs_it():
    events = []
    # limit=1: "quick" waits in the queue behind "slow" and is cancelled there.
    inbound = [request(1, "slow"), request(2, "quick"), cancel(2)]
    out = await run(demo_server(events), inbound)
    assert [message["id"] for message in out] == [1]
    assert "quick ran" not in events


async def test_cancelling_a_finished_unwritten_reply_drops_it():
    # Reply 0 holds the writer, so reply 1 is complete but waits unwritten
    # when its cancellation arrives.
    inbound = [request(0, "quick"), request(1, "quick"), cancel(1)]
    out = await run(demo_server([]), inbound, hold_first_write=True)
    assert [message["id"] for message in out] == [0]


async def test_a_handler_swallowing_the_cancellation_still_sends_nothing():
    events = []
    out = await run(demo_server(events), [request(1, "stubborn"), cancel(1)])
    assert out == []
    assert events == ["stubborn swallowed"]


async def test_unknown_finished_and_malformed_cancellations_change_nothing():
    inbound = [
        request(1, "quick"),
        cancel(1),  # already answered
        cancel(99),  # never seen
        {"jsonrpc": "2.0", "method": "notifications/cancelled"},
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": [1]},
        },
    ]
    out = await run(demo_server([]), inbound)
    assert out == [{"jsonrpc": "2.0", "id": 1, "result": {}}]


async def test_an_id_already_in_flight_is_rejected():
    inbound = [request(1, "slow"), request(1, "quick")]
    out = await run(demo_server([]), inbound)
    assert out[0]["id"] == 1
    assert out[0]["error"]["code"] == jsonrpc.INVALID_REQUEST
    assert out[1] == {"jsonrpc": "2.0", "id": 1, "result": {}}


async def test_ids_7_and_quoted_7_are_different_requests():
    inbound = [request(7, "slow"), request("7", "quick"), cancel(7)]
    out = await run(demo_server([]), inbound)
    assert out == [{"jsonrpc": "2.0", "id": "7", "result": {}}]
