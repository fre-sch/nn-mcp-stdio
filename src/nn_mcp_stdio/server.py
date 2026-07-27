"""The stdio MCP server: register handler functions, run the transport loop.

Pipeline: one reader parses each line and dispatches by message shape; an
aiojobs Scheduler runs handlers concurrently
and in isolation; handlers enqueue their replies onto an outbound queue that a
single writer task drains to the transport. Logging goes to stderr.
"""

import asyncio
import dataclasses
import json
import logging
import pathlib
import typing

import aiojobs

from nn_mcp_types import jsonrpc, lifecycle
from nn_mcp_types import logging as mcp_logging
from nn_mcp_types import resources as resource_types
from nn_mcp_types import tools as tool_types
from nn_mcp_types.wire import from_wire, to_wire

from nn_mcp_stdio import errors, resources, tools
from nn_mcp_stdio.context import Context, context_parameter
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
        self._resources = {}  # uri -> registered Resource
        self._client = None  # the peer's Implementation, captured at initialize
        self._log_level = None  # last logging/setLevel; filtering deferred
        self._outbox = None  # the outbound queue, live for the run() loop
        # handler -> its Context parameter name (detected once, then cached)
        self._context_names = {}
        self._request_handlers = {
            lifecycle.INITIALIZE: self._initialize,
            mcp_logging.LOGGING_SET_LEVEL: self._set_level,
            tool_types.TOOLS_LIST: self._list_tools,
            tool_types.TOOLS_CALL: self._call_tool,
            resource_types.RESOURCES_LIST: self._list_resources,
            resource_types.RESOURCES_READ: self._read_resource,
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
        structured_content: bool = False,
    ) -> typing.Callable:
        """Register an `async def` handler as a tool.

        Usable bare (`@server.tool`) or with options
        (`@server.tool(name=..., strict_arguments=...)`). The `inputSchema` is
        derived from the handler's annotations; the docstring is the tool
        description. `strict_arguments` (default `True`) rejects unknown
        arguments. `structured_content` (default `False`) opts the tool into
        structured output: its return (a dataclass/`dict`) becomes
        `structuredContent`, and a dataclass return annotation derives the
        `outputSchema`.
        """

        def register(handler):
            built = tools.build_tool(
                handler,
                name=name,
                description=description,
                strict_arguments=strict_arguments,
                structured_content=structured_content,
            )
            self._tools[built.definition.name] = built
            return handler

        if function is not None:  # bare @server.tool
            return register(function)
        return register  # @server.tool(...)

    def resource(self, definition: resource_types.Resource) -> typing.Callable:
        """Register an `async def` reader as a readable resource.

        `definition` is the full `Resource`: its `uri` is the resource's identity
        and the `resources/read` key, and it is listed verbatim (a reader
        describes only its contents, so the decorator carries all the metadata --
        `title`, `annotations`, `meta`, ...). A resource takes no arguments
        (search is a tool, not a resource). The reader returns the contents: a
        `str`/`bytes` is wrapped, filling `uri`/`mimeType` from `definition`; a
        `*ResourceContents` (or list) or `ReadResourceResult` is used as-is. It
        may declare a `Context` parameter. Registering a resource advertises the
        `resources` capability at `initialize`.
        """

        def register(reader):
            self._register_resource(
                resources.build_resource(reader, definition)
            )
            return reader

        return register

    def add_resource_from_literal(
        self,
        definition: resource_types.Resource,
        content: str | bytes,
    ) -> None:
        """Register a static resource whose contents are a fixed literal.

        `definition` is the full `Resource` (uri, name, mimeType, ...) -- listed
        verbatim, read by `definition.uri`. `content` is served on every read: a
        `str` as text, `bytes` as a base64 blob, with uri/mimeType from
        `definition`. Registering a resource advertises the `resources`
        capability at `initialize`.
        """
        self._register_resource(
            resources.build_literal_resource(definition, content)
        )

    def add_resource_from_path(
        self,
        definition: resource_types.Resource,
        path: pathlib.Path,
        *,
        describe_contents: resources.DescribeContents | None = None,
    ) -> None:
        """Register a static resource whose contents are read from a file.

        `definition` is the full `Resource`, listed verbatim and read by
        `definition.uri`; the file at `path` is read lazily on every read.
        `describe_contents(path, data)` classifies the bytes -- returning the
        MIME type and the contents block type (`TextResourceContents` or
        `BlobResourceContents`) -- and so overrides `definition.mime_type` for
        the served content; without it the file is served as a base64 blob typed
        by `definition.mime_type`. Registering a resource advertises the
        `resources` capability at `initialize`.
        """
        self._register_resource(
            resources.build_path_resource(
                definition, path, describe_contents=describe_contents
            )
        )

    def _register_resource(self, built: resources.Resource) -> None:
        self._resources[built.definition.uri] = built

    async def _initialize(self, params):
        params = params or {}
        client = params.get("clientInfo")
        if client is not None:
            self._client = from_wire(lifecycle.Implementation, client)
        return lifecycle.InitializeResult(
            protocol_version=lifecycle.PROTOCOL_VERSION,
            capabilities=self._effective_capabilities(),
            server_info=self._implementation,
        )

    def _effective_capabilities(self):
        # Handlers can always log through Context, so advertise `logging`;
        # registering tools advertises `tools`. Either yields to a capability
        # the caller declared themselves.
        changes = {}
        if self._capabilities.logging is None:
            changes["logging"] = {}
        if self._tools and self._capabilities.tools is None:
            changes["tools"] = lifecycle.ToolsCapability()
        if self._resources and self._capabilities.resources is None:
            changes["resources"] = lifecycle.ResourcesCapability()
        if changes:
            return dataclasses.replace(self._capabilities, **changes)
        return self._capabilities

    async def _set_level(self, params):
        # Accept the client's minimum level and acknowledge. Filtering by it is
        # deferred; we store it for later.
        self._log_level = (params or {}).get("level")
        return {}

    async def _list_tools(self, params):
        return tool_types.ListToolsResult(
            tools=[tool.definition for tool in self._tools.values()]
        )

    async def _call_tool(self, params, context: Context):
        params = params or {}
        tool = self._tools.get(params.get("name"))
        if tool is None:
            raise errors.invalid_params(f"unknown tool: {params.get('name')!r}")
        return await tool.call(params.get("arguments"), context)

    async def _list_resources(self, params):
        return resource_types.ListResourcesResult(
            resources=[
                resource.definition for resource in self._resources.values()
            ]
        )

    async def _read_resource(self, params, context: Context):
        params = params or {}
        uri = params.get("uri")
        resource = self._resources.get(uri)
        if resource is None:
            raise errors.resource_not_found(uri)
        return await resource.read(context)

    async def run(self, transport: Transport | None = None) -> None:
        transport = transport or StdioTransport()
        outbox = asyncio.Queue()
        self._outbox = outbox  # reachable from Context for the loop's lifetime
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
        context = self._make_context(message["id"], message.get("params"))
        try:
            result = await self._invoke(handler, message.get("params"), context)
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
        context = self._make_context(None, message.get("params"))
        try:
            await self._invoke(handler, message.get("params"), context)
        except Exception:
            log.exception("notification handler %r failed", message["method"])

    async def _invoke(self, handler, params, context):
        # Call the handler with `params`, plus the Context injected under the
        # name the handler chose for it (if any).
        name = self._context_name(handler)
        if name is None:
            return await handler(params)
        return await handler(params, **{name: context})

    def _context_name(self, handler):
        if handler not in self._context_names:
            self._context_names[handler] = context_parameter(handler)
        return self._context_names[handler]

    def _make_context(self, request_id, params):
        token = None
        if isinstance(params, dict) and isinstance(params.get("_meta"), dict):
            token = params["_meta"].get("progressToken")
        return Context(
            request_id=request_id,
            client=self._client,
            progress_token=token,
            outbox=self._outbox,
            encode=self._encode,
        )

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
