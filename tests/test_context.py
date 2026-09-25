"""Context injection: logging, progress, request metadata, and capability."""

import asyncio
import json

from nn_mcp_stdio import Context, Server
from nn_mcp_stdio.transport import MemoryTransport


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


def call(name, arguments, *, meta=None, request_id=2):
    params = {"name": name, "arguments": arguments}
    if meta is not None:
        params["_meta"] = meta
    return request("tools/call", params, request_id=request_id)


def announcer():
    server = Server(name="demo", version="0.1.0")

    @server.tool()
    async def announce(text: str, ctx: Context) -> str:
        """Announce a message."""
        await ctx.info(f"got {text}", logger="announce")
        return "ok"

    return server


async def test_context_parameter_is_absent_from_input_schema():
    out = await run(
        announcer(),
        [encode({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})],
    )
    (tool,) = out[0]["result"]["tools"]
    # `ctx` is injected, not a wire argument -- only `text` is in the schema.
    assert tool["inputSchema"]["properties"] == {"text": {"type": "string"}}
    assert tool["inputSchema"]["required"] == ["text"]


async def test_tool_logging_emits_message_before_result():
    out = await run(announcer(), [call("announce", {"text": "hi"})])
    # the notification is enqueued mid-handler, so it precedes the response.
    assert out[0] == {
        "jsonrpc": "2.0",
        "method": "notifications/message",
        "params": {"level": "info", "data": "got hi", "logger": "announce"},
    }
    assert out[1]["result"]["content"] == [{"type": "text", "text": "ok"}]


def worker():
    server = Server(name="demo", version="0.1.0")

    @server.tool()
    async def work(ctx: Context) -> str:
        await ctx.report_progress(0.5, total=1.0, message="half")
        return "done"

    return server


async def test_progress_emitted_when_token_supplied():
    out = await run(
        worker(),
        [call("work", {}, meta={"progressToken": "p1"})],
    )
    assert out[0] == {
        "jsonrpc": "2.0",
        "method": "notifications/progress",
        "params": {
            "progressToken": "p1",
            "progress": 0.5,
            "total": 1.0,
            "message": "half",
        },
    }
    assert out[1]["result"]["content"] == [{"type": "text", "text": "done"}]


async def test_progress_is_a_noop_without_token():
    out = await run(worker(), [call("work", {})])
    # no progressToken -> no progress notification, just the result.
    assert len(out) == 1
    assert out[0]["result"]["content"] == [{"type": "text", "text": "done"}]


async def test_raw_request_handler_receives_context():
    server = Server(name="demo", version="0.1.0")

    @server.request("whoami")
    async def whoami(params, ctx: Context):
        return {"rid": ctx.request_id}

    out = await run(server, [request("whoami", request_id=9)])
    assert out[0]["result"] == {"rid": 9}


async def test_context_carries_client_from_initialize():
    server = Server(name="demo", version="0.1.0")

    @server.request("whoami")
    async def whoami(params, ctx: Context):
        return {"client": ctx.client.name}

    initialize = encode(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "acme", "version": "1"},
            },
        }
    )
    out = await run(server, [initialize, request("whoami", request_id=2)])
    assert out[1]["result"] == {"client": "acme"}


async def test_initialize_advertises_logging_capability():
    server = Server(name="demo", version="0.1.0")
    out = await run(
        server,
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
    assert "logging" in out[0]["result"]["capabilities"]


async def test_set_level_is_acknowledged():
    server = Server(name="demo", version="0.1.0")
    out = await run(
        server,
        [request("logging/setLevel", {"level": "warning"}, request_id=3)],
    )
    assert out[0] == {"jsonrpc": "2.0", "id": 3, "result": {}}


def threaded_logger(errors):
    server = Server(name="demo", version="0.1.0")

    def log_from_own_loop(ctx):
        try:
            asyncio.run(ctx.info("from a thread"))
        except RuntimeError as error:
            errors.append(str(error))
            raise

    @server.tool()
    async def threaded(ctx: Context) -> str:
        await asyncio.to_thread(log_from_own_loop, ctx)
        return "unreachable"

    @server.tool()
    async def plain() -> str:
        return "still serving"

    return server


async def test_context_refuses_another_event_loop():
    errors = []
    out = await run(
        threaded_logger(errors),
        [call("threaded", {}, request_id=1), call("plain", {}, request_id=2)],
    )
    assert "outside the event loop it belongs to" in errors[0]
    # nothing was emitted from the thread -- only the two replies went out.
    assert [message.get("method") for message in out] == [None, None]
    replies = {message["id"]: message["result"] for message in out}
    assert replies[1]["isError"] is True  # the tool reports its failure...
    # ...and the server keeps serving.
    assert replies[2]["content"][0]["text"] == "still serving"


async def test_context_refuses_another_event_loop_in_debug_mode():
    # Without the check, debug mode turned the race into a hung server.
    asyncio.get_running_loop().set_debug(True)
    inbound = [
        call("threaded", {}, request_id=1),
        call("plain", {}, request_id=2),
    ]
    out = await asyncio.wait_for(run(threaded_logger([]), inbound), timeout=5)
    assert sorted(message["id"] for message in out) == [1, 2]
