"""context.to_thread: blocking work in a thread, talking back safely."""

import asyncio
import json
import threading
import time

from support import cancel, run

from nn_mcp_stdio import Context, ContextThreadSafe, Server


def call(name, request_id=1, meta=None):
    params = {"name": name, "arguments": {}}
    if meta is not None:
        params["_meta"] = meta
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": params,
    }


def calculate(parts, reporter: ContextThreadSafe):
    for step in range(parts):
        reporter.info(f"part {step}")
        reporter.report_progress(step + 1, parts)
    return f"{parts} parts"


def thread_server():
    server = Server(name="demo", version="0.1.0")

    @server.tool()
    async def heavy(context: Context) -> str:
        return await context.to_thread(calculate, 3)

    @server.tool()
    async def plain_function(context: Context) -> str:
        return await context.to_thread(lambda text: text.upper(), "no context")

    return server


def kinds(out):
    return [message.get("method") or "reply" for message in out]


async def test_thread_messages_arrive_in_order_before_the_reply():
    asyncio.get_running_loop().set_debug(True)  # flags any unsafe call
    out = await run(thread_server(), [call("heavy", meta={"progressToken": 7})])
    assert kinds(out) == [
        "notifications/message",
        "notifications/progress",
    ] * 3 + ["reply"]
    logged = [
        m["params"]["data"]
        for m in out
        if m.get("method") == "notifications/message"
    ]
    assert logged == ["part 0", "part 1", "part 2"]
    progress = [
        m["params"]["progress"]
        for m in out
        if m.get("method") == "notifications/progress"
    ]
    assert progress == [1, 2, 3]
    assert out[-1]["result"]["content"][0]["text"] == "3 parts"


async def test_progress_without_a_token_is_a_no_op():
    out = await run(thread_server(), [call("heavy")])
    assert "notifications/progress" not in kinds(out)


async def test_function_without_the_parameter_is_called_without_it():
    out = await run(thread_server(), [call("plain_function")])
    assert out[0]["result"]["content"][0]["text"] == "NO CONTEXT"


async def test_the_parameter_is_found_by_annotation_under_any_name():
    seen = {}

    def inspect(*, whatever_name: ContextThreadSafe):
        seen["request_id"] = whatever_name.request_id
        seen["type"] = type(whatever_name)

    server = Server(name="demo", version="0.1.0")

    @server.tool()
    async def probe(context: Context) -> str:
        await context.to_thread(inspect)
        return "ok"

    await run(server, [call("probe", request_id=42)])
    assert seen == {"request_id": 42, "type": ContextThreadSafe}


async def test_cancellation_ends_the_await_and_reaches_the_thread():
    events = []
    finished = threading.Event()

    def compute(context: ContextThreadSafe):
        context.raise_if_cancelled()  # not cancelled yet: returns
        events.append(("before", context.cancelled))
        deadline = time.monotonic() + 2
        while not context.cancelled and time.monotonic() < deadline:
            time.sleep(0.005)
        events.append(("after", context.cancelled))
        try:
            context.raise_if_cancelled()
        except asyncio.CancelledError:
            events.append(("raised", True))
        finished.set()

    server = Server(name="demo", version="0.1.0")

    @server.tool()
    async def long_running(context: Context) -> str:
        try:
            return await context.to_thread(compute)
        except asyncio.CancelledError:
            events.append(("handler cancelled", finished.is_set()))
            raise

    out = await run(server, [call("long_running"), cancel(1)])
    await asyncio.to_thread(finished.wait, 2)
    assert out == []  # a cancelled request gets no reply
    assert ("before", False) in events
    assert ("after", True) in events
    assert ("raised", True) in events
    # the handler's await ended while the thread was still running
    assert ("handler cancelled", False) in events
