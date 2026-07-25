"""The stdio MCP server: register handler functions, run the transport loop.

Pipeline (see wiki stdio-server-concurrency): one reader parses each line and
dispatches by message shape; an aiojobs Scheduler runs handlers concurrently
and in isolation; handlers enqueue their replies onto an outbound queue that a
single writer task drains to the transport. Logging goes to stderr.
"""

import asyncio
import json
import logging

import aiojobs

from nn_mcp_types import jsonrpc, lifecycle
from nn_mcp_types.wire import to_wire

from nn_mcp_stdio.transport import StdioTransport

log = logging.getLogger("nn_mcp_stdio")


class Server:
    """An MCP server. Register handlers, then `await server.run()`.

    `initialize` is answered from `name`/`version`/`capabilities`; register the
    rest with `@server.request(method)` and `@server.notification(method)`.
    """

    def __init__(self, name, version, capabilities=None, *, limit=100):
        self._implementation = lifecycle.Implementation(
            name=name, version=version
        )
        self._capabilities = capabilities or lifecycle.ServerCapabilities()
        self._limit = limit
        self._request_handlers = {lifecycle.INITIALIZE: self._initialize}
        self._notification_handlers = {}

    def request(self, method):
        """Register a coroutine `handler(params) -> result` for a method."""

        def register(handler):
            self._request_handlers[method] = handler
            return handler

        return register

    def notification(self, method):
        """Register a coroutine `handler(params) -> None` for a notification."""

        def register(handler):
            self._notification_handlers[method] = handler
            return handler

        return register

    async def _initialize(self, params):
        return lifecycle.InitializeResult(
            protocol_version=lifecycle.PROTOCOL_VERSION,
            capabilities=self._capabilities,
            server_info=self._implementation,
        )

    async def run(self, transport=None):
        transport = transport or StdioTransport()
        outbox = asyncio.Queue()
        writer = asyncio.create_task(self._write_outbox(transport, outbox))
        scheduler = aiojobs.Scheduler(
            limit=self._limit, exception_handler=self._on_job_error
        )
        try:
            await self._read_messages(transport, outbox, scheduler)
        finally:
            await scheduler.wait_and_close()  # let in-flight handlers finish
            await outbox.join()  # flush their replies
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)

    async def _read_messages(self, transport, outbox, scheduler):
        while True:
            line = await transport.read_line()
            if line is None:
                break  # stdin EOF -> shut down
            await self._dispatch(line, outbox, scheduler)

    async def _dispatch(self, line, outbox, scheduler):
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            await outbox.put(
                self._failure(None, jsonrpc.PARSE_ERROR, "parse error")
            )
            return
        if not isinstance(message, dict):
            await outbox.put(
                self._failure(None, jsonrpc.INVALID_REQUEST, "invalid request")
            )
            return
        if "method" not in message:
            # A response to a server-initiated request -- not handled yet.
            log.debug("ignoring inbound response: %s", message)
            return
        if "id" in message:
            await scheduler.spawn(self._answer(message, outbox))
        else:
            await scheduler.spawn(self._notify(message))

    async def _answer(self, message, outbox):
        method = message["method"]
        handler = self._request_handlers.get(method)
        if handler is None:
            await outbox.put(
                self._failure(
                    message["id"],
                    jsonrpc.METHOD_NOT_FOUND,
                    f"method not found: {method}",
                )
            )
            return
        try:
            result = await handler(message.get("params"))
        except Exception:
            log.exception("request handler %r failed", method)
            await outbox.put(
                self._failure(
                    message["id"], jsonrpc.INTERNAL_ERROR, "internal error"
                )
            )
            return
        await outbox.put(
            self._encode(jsonrpc.Response(id=message["id"], result=result))
        )

    async def _notify(self, message):
        handler = self._notification_handlers.get(message["method"])
        if handler is None:
            log.debug("no handler for notification %r", message["method"])
            return
        try:
            await handler(message.get("params"))
        except Exception:
            log.exception("notification handler %r failed", message["method"])

    async def _write_outbox(self, transport, outbox):
        while True:
            line = await outbox.get()
            try:
                await transport.write_line(line)
            finally:
                outbox.task_done()

    def _encode(self, envelope):
        return json.dumps(
            to_wire(envelope), ensure_ascii=False, separators=(",", ":")
        )

    def _failure(self, request_id, code, text):
        return self._encode(
            jsonrpc.ErrorResponse(
                error=jsonrpc.Error(code=code, message=text), id=request_id
            )
        )

    def _on_job_error(self, scheduler, context):
        log.error("job error: %r", context.get("exception"))
