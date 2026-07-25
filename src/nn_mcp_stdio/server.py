"""The stdio MCP server: register handler functions, run the transport loop.

Pipeline (see wiki stdio-server-concurrency): one reader parses each line and
dispatches by message shape; an aiojobs Scheduler runs handlers concurrently
and in isolation; handlers enqueue their replies onto an outbound queue that a
single writer task drains to the transport. Logging goes to stderr.
"""

import asyncio
import dataclasses
import json
import logging
import typing

import aiojobs

from nn_mcp_types import jsonrpc, lifecycle
from nn_mcp_types import tools as tool_types
from nn_mcp_types.wire import to_wire

from nn_mcp_stdio import errors, tools
from nn_mcp_stdio.transport import StdioTransport, Transport

log = logging.getLogger("nn_mcp_stdio")


class Server:
    """An MCP server. Register handlers, then `await server.run()`.

    `initialize` is answered from `name`/`version`/`capabilities`. Expose tools
    with `@server.tool` (annotated `async def`s -- `tools/list` and a strictly
    validated `tools/call` are built in), and register any other method with
    `@server.request(method)` or `@server.notification(method)`.
    """

    def __init__(
        self,
        name: str,
        version: str,
        capabilities: lifecycle.ServerCapabilities | None = None,
        *,
        limit: int = 100,
    ) -> None:
        self._implementation = lifecycle.Implementation(
            name=name, version=version
        )
        self._capabilities = capabilities or lifecycle.ServerCapabilities()
        self._limit = limit
        self._tools = {}
        self._request_handlers = {
            lifecycle.INITIALIZE: self._initialize,
            tool_types.TOOLS_LIST: self._list_tools,
            tool_types.TOOLS_CALL: self._call_tool,
        }
        self._notification_handlers = {}

    def request(self, method: str) -> typing.Callable:
        """Register a coroutine `handler(params) -> result` for a method."""

        def register(handler):
            self._request_handlers[method] = handler
            return handler

        return register

    def notification(self, method: str) -> typing.Callable:
        """Register a coroutine `handler(params) -> None` for a notification."""

        def register(handler):
            self._notification_handlers[method] = handler
            return handler

        return register

    def tool(
        self,
        function: typing.Callable | None = None,
        *,
        name: str | None = None,
        description: str | None = None,
        strict_arguments: bool = True,
    ) -> typing.Callable:
        """Register an `async def` handler as a tool.

        Usable bare (`@server.tool`) or with options
        (`@server.tool(name=..., strict_arguments=...)`). The `inputSchema` is
        derived from the handler's annotations; the docstring is the tool
        description. `strict_arguments` (default `True`) rejects unknown
        arguments.
        """

        def register(handler):
            built = tools.build_tool(
                handler,
                name=name,
                description=description,
                strict_arguments=strict_arguments,
            )
            self._tools[built.definition.name] = built
            return handler

        if function is not None:  # bare @server.tool
            return register(function)
        return register  # @server.tool(...)

    async def _initialize(self, params):
        return lifecycle.InitializeResult(
            protocol_version=lifecycle.PROTOCOL_VERSION,
            capabilities=self._effective_capabilities(),
            server_info=self._implementation,
        )

    def _effective_capabilities(self):
        # Registering tools advertises the `tools` capability, unless the caller
        # already declared one of their own.
        if self._tools and self._capabilities.tools is None:
            return dataclasses.replace(
                self._capabilities, tools=lifecycle.ToolsCapability()
            )
        return self._capabilities

    async def _list_tools(self, params):
        return tool_types.ListToolsResult(
            tools=[tool.definition for tool in self._tools.values()]
        )

    async def _call_tool(self, params):
        params = params or {}
        tool = self._tools.get(params.get("name"))
        if tool is None:
            raise errors.invalid_params(f"unknown tool: {params.get('name')!r}")
        return await tool.call(params.get("arguments"))

    async def run(self, transport: Transport | None = None) -> None:
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
        except errors.RequestError as error:
            await outbox.put(
                self._failure(message["id"], error.code, error.message)
            )
            return
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
