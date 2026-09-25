"""The stdio MCP server: register handler functions, run the transport loop.

Pipeline: the read loop parses each line into a validated envelope and
dispatches it by kind; an aiojobs Scheduler runs handlers concurrently and in
isolation; handlers enqueue their replies onto an outbound queue that a
single writer task drains to the transport. A request the server sends to the
client (elicitation) waits on a future the read loop resolves from the client's
response. Logging goes to stderr.
"""

import asyncio
import dataclasses
import itertools
import json
import logging
import pathlib
import typing

import aiojobs
from jsonschema.exceptions import best_match
from jsonschema.validators import Draft202012Validator
from nn_rfc6570_router import LiteralRoute, Router, TemplateRoute

from nn_mcp_types import common, jsonrpc, lifecycle
from nn_mcp_types import logging as mcp_logging
from nn_mcp_types import resources as resource_types
from nn_mcp_types import tools as tool_types
from nn_mcp_types.schema import get_schema
from nn_mcp_types.wire import from_wire, to_wire

from nn_mcp_stdio import envelope, errors, pagination, resources, tools
from nn_mcp_stdio.context import Context, context_parameter
from nn_mcp_stdio.transport import StdioTransport, Transport

log = logging.getLogger("nn_mcp_stdio")

_CANCELLED_VALIDATOR = Draft202012Validator(
    get_schema(common.CancelledNotificationParams)
)
_CAPABILITIES_VALIDATOR = Draft202012Validator(
    get_schema(lifecycle.ClientCapabilities)
)


class Reply(typing.NamedTuple):
    """A reply to an accepted request, written only while it is still owed.

    Every other outbound line -- notifications, rejections of malformed or
    duplicate requests -- is written unconditionally.
    """

    request_name: str
    line: str


def request_name(request_id: int | str) -> str:
    """The name a request goes by: its job's name and its key in the owed set.

    `json.dumps`, not `str`, keeps the ids `7` and `"7"` apart.
    """
    return json.dumps(request_id)


class Server:
    """An MCP server. Register handlers, then `await server.run()`.

    `initialize` is answered from `name`/`version`/`capabilities`. Expose tools
    with `@server.tool` (annotated `async def`s -- `tools/list` and a strictly
    validated `tools/call` are built in), and register any other method with
    `@server.request(method)` or `@server.notification(method)`.

    `limit` bounds how many handlers run at once. `page_size` is how many items
    a list endpoint returns per page; `None` disables pagination.
    """

    def __init__(
        self,
        name: str,
        version: str,
        capabilities: lifecycle.ServerCapabilities | None = None,
        *,
        limit: int = 100,
        page_size: int | None = 100,
    ) -> None:
        if page_size is not None and page_size < 1:
            raise ValueError(f"page_size must be positive or None: {page_size}")
        self._implementation = lifecycle.Implementation(
            name=name, version=version
        )
        self._capabilities = capabilities or lifecycle.ServerCapabilities()
        self._limit = limit
        self._page_size = page_size
        self._tools = {}
        # The route table owns the resource set: direct resources and templates
        # both register here, a read resolves to the most-specific match (so a
        # direct resource shadows an overlapping template), and the two list
        # endpoints iterate it -- no shadow copy of the registrations.
        self._resource_router = Router()
        self._client = None  # the peer's Implementation, captured at initialize
        self._client_capabilities = None  # likewise its ClientCapabilities
        self._log_level = None  # last logging/setLevel; filtering deferred
        self._outbox = None  # the outbound queue, live for the run() loop
        # Names of accepted requests whose reply is not yet written. A
        # cancellation discards the name, so the writer drops the reply.
        self._owed = set()
        self._closing = set()  # job.close() tasks, kept referenced until done
        # Requests the server sends: ids of their own, and a future per request
        # awaiting the client's answer, resolved by the read loop.
        self._request_ids = itertools.count(1)
        self._pending = {}
        self._input_closed = False  # stdin reached EOF: nobody will answer
        # handler -> its Context parameter name (detected once, then cached)
        self._context_names = {}
        self._request_handlers = {
            lifecycle.INITIALIZE: self._initialize,
            mcp_logging.LOGGING_SET_LEVEL: self._set_level,
            tool_types.TOOLS_LIST: self._list_tools,
            tool_types.TOOLS_CALL: self._call_tool,
            resource_types.RESOURCES_LIST: self._list_resources,
            resource_types.RESOURCES_TEMPLATES_LIST: self._list_resource_templates,
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

    def resource_template(
        self, definition: resource_types.ResourceTemplate
    ) -> typing.Callable:
        """Register an `async def` reader as a resource template.

        `definition` is the full `ResourceTemplate`: its `uri_template` (RFC 6570)
        is advertised verbatim by `resources/templates/list` and becomes a route.
        A `resources/read` of any URI the template matches invokes the reader with
        the template's `{vars}` extracted from the URI and injected by name
        (mirroring tool arguments); the reader may also declare a `Context`
        parameter. The reader returns the contents like any resource reader. A
        direct resource whose URI the template also matches shadows it (routing is
        most-specific-wins). Registering a template advertises the `resources`
        capability at `initialize`.
        """

        def register(reader):
            self._register_resource_template(
                resources.build_template_resource(reader, definition)
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
        self._resource_router.add(built.definition.uri, built)

    def _register_resource_template(self, built: resources.Resource) -> None:
        # `add` rejects a uriTemplate with an unsupported RFC 6570 operator here,
        # at registration -- a template the router cannot reverse is never
        # advertised.
        self._resource_router.add(built.definition.uri_template, built)

    async def _initialize(self, params):
        params = params or {}
        client = params.get("clientInfo")
        if client is not None:
            self._client = from_wire(lifecycle.Implementation, client)
        self._client_capabilities = self._declared_capabilities(params)
        return lifecycle.InitializeResult(
            protocol_version=lifecycle.PROTOCOL_VERSION,
            capabilities=self._effective_capabilities(),
            server_info=self._implementation,
        )

    def _declared_capabilities(self, params):
        capabilities = params.get("capabilities", {})
        invalid = best_match(_CAPABILITIES_VALIDATOR.iter_errors(capabilities))
        if invalid is not None:
            raise errors.invalid_params(
                f"invalid capabilities: {invalid.message}"
            )
        return from_wire(lifecycle.ClientCapabilities, capabilities)

    def _effective_capabilities(self):
        # Handlers can always log through Context, so advertise `logging`;
        # registering tools advertises `tools`. Either yields to a capability
        # the caller declared themselves.
        changes = {}
        if self._capabilities.logging is None:
            changes["logging"] = {}
        if self._tools and self._capabilities.tools is None:
            changes["tools"] = lifecycle.ToolsCapability()
        if len(self._resource_router) and self._capabilities.resources is None:
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
        definitions = [tool.definition for tool in self._tools.values()]
        page, next_cursor = pagination.page(
            definitions, (params or {}).get("cursor"), self._page_size
        )
        return tool_types.ListToolsResult(tools=page, next_cursor=next_cursor)

    async def _call_tool(self, params, context: Context):
        params = params or {}
        tool = self._tools.get(params.get("name"))
        if tool is None:
            raise errors.invalid_params(f"unknown tool: {params.get('name')!r}")
        return await tool.call(params.get("arguments"), context)

    async def _list_resources(self, params):
        # Direct resources are the router's literal routes: a `Resource`'s uri is
        # a concrete URI, and a template must carry parameters, so the two
        # discovery surfaces correspond exactly to the two route kinds.
        page, next_cursor = self._page_of_routes(LiteralRoute, params)
        return resource_types.ListResourcesResult(
            resources=page, next_cursor=next_cursor
        )

    async def _list_resource_templates(self, params):
        page, next_cursor = self._page_of_routes(TemplateRoute, params)
        return resource_types.ListResourceTemplatesResult(
            resource_templates=page, next_cursor=next_cursor
        )

    def _page_of_routes(self, route_kind, params):
        # The route table's order is stable: literal routes in registration
        # order, templates most-specific-first.
        definitions = [
            route.value.definition
            for route in self._resource_router
            if isinstance(route, route_kind)
        ]
        return pagination.page(
            definitions, (params or {}).get("cursor"), self._page_size
        )

    async def _read_resource(self, params, context: Context):
        params = params or {}
        uri = params.get("uri")
        matched = (
            self._resource_router.match(uri) if isinstance(uri, str) else None
        )
        if matched is None:
            raise errors.resource_not_found(uri)
        resource, variables = matched
        return await resource.read(uri, variables, context)

    async def run(self, transport: Transport | None = None) -> None:
        transport = transport or StdioTransport()
        outbox = asyncio.Queue()
        self._outbox = outbox  # reachable from Context for the loop's lifetime
        self._input_closed = False
        writer = asyncio.create_task(self._write_outbox(transport, outbox))
        scheduler = aiojobs.Scheduler(
            limit=self._limit, exception_handler=self._on_job_error
        )
        try:
            await self._read_messages(transport, outbox, scheduler)
        finally:
            self._abandon_pending_requests()
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
            message = envelope.parse(line)
        except envelope.ParseError:
            await outbox.put(
                self._failure(None, jsonrpc.PARSE_ERROR, "parse error")
            )
            return
        except envelope.InvalidMessage as error:
            await self._reject(error, outbox)
            return
        if isinstance(message, jsonrpc.Request):
            await self._accept(message, outbox, scheduler)
        elif isinstance(message, jsonrpc.Notification):
            await self._receive(message, scheduler)
        else:
            self._resolve(message)

    async def _accept(self, request, outbox, scheduler):
        name = request_name(request.id)
        if name in self._owed:
            await outbox.put(
                self._failure(
                    request.id,
                    jsonrpc.INVALID_REQUEST,
                    "invalid request: id already in flight",
                )
            )
            return
        if request.method == common.PING:
            await self._answer_ping(request, outbox)
            return
        self._owed.add(name)
        await scheduler.spawn(self._answer(request, outbox), name=name)

    async def _answer_ping(self, request, outbox):
        # A liveness check answered from the read loop: queued behind busy
        # jobs, it would make a healthy server look dead.
        await outbox.put(
            self._encode(
                jsonrpc.Response(
                    id=request.id,
                    result=common.EmptyResult(),
                    jsonrpc=jsonrpc.VERSION,
                )
            )
        )

    async def _receive(self, notification, scheduler):
        # A cancellation runs in the read loop rather than as a job: a job
        # could wait in the scheduler's queue behind the job it cancels.
        if notification.method == common.CANCELLED:
            self._cancel(notification.params, scheduler)
            return
        await scheduler.spawn(self._notify(notification))

    def _cancel(self, params, scheduler):
        name = self._cancelled_request_name(params)
        if name is None:
            return
        self._owed.discard(name)
        job = next((job for job in scheduler if job.get_name() == name), None)
        if job is not None:  # otherwise unknown or already finished
            self._close_off_loop(job)

    def _cancelled_request_name(self, params):
        params = params or {}
        invalid = best_match(_CANCELLED_VALIDATOR.iter_errors(params))
        if invalid is not None:
            log.warning("ignoring malformed cancellation: %s", invalid.message)
            return None
        cancelled = from_wire(common.CancelledNotificationParams, params)
        if cancelled.request_id is None:
            log.warning("ignoring cancellation without a requestId")
            return None
        return request_name(cancelled.request_id)

    def _close_off_loop(self, job):
        # close() waits for the handler; awaited here it would stall the loop.
        closing = asyncio.create_task(job.close())
        self._closing.add(closing)
        closing.add_done_callback(self._closed)

    def _closed(self, closing):
        self._closing.discard(closing)
        if not closing.cancelled() and closing.exception() is not None:
            log.error("closing a cancelled job failed: %r", closing.exception())

    async def _request(self, method, params):
        # Send a request to the client and return its result. A cancelled
        # caller, or EOF, takes the pending entry with it.
        if self._input_closed:
            raise asyncio.CancelledError("stdin closed: nobody will answer")
        request_id = next(self._request_ids)
        answered = asyncio.get_running_loop().create_future()
        self._pending[request_id] = answered
        try:
            await self._outbox.put(
                self._encode(
                    jsonrpc.Request(
                        method=method,
                        id=request_id,
                        params=params,
                        jsonrpc=jsonrpc.VERSION,
                    )
                )
            )
            return await answered
        finally:
            del self._pending[request_id]

    def _resolve(self, response):
        answered = self._pending.get(response.id)
        if answered is None or answered.done():
            log.warning("ignoring response to no pending request: %s", response)
            return
        if isinstance(response, jsonrpc.ErrorResponse):
            answered.set_exception(
                errors.ClientError(response.error.code, response.error.message)
            )
        else:
            answered.set_result(response.result)

    def _abandon_pending_requests(self):
        # At EOF nobody will answer, and no reply can reach the client: the
        # waiting handlers see CancelledError, as for a cancelled request.
        self._input_closed = True
        for answered in self._pending.values():
            answered.cancel()

    async def _reject(self, error, outbox):
        # A malformed response is logged, never answered: replying to a
        # response could set two peers answering each other's errors.
        if error.envelope_type in envelope.RESPONSE_TYPES:
            log.warning("ignoring malformed inbound response: %s", error.reason)
            return
        await outbox.put(
            self._failure(
                error.request_id,
                jsonrpc.INVALID_REQUEST,
                f"invalid request: {error.reason}",
            )
        )

    async def _answer(self, request, outbox):
        await outbox.put(
            Reply(request_name(request.id), await self._outcome(request))
        )

    async def _outcome(self, request):
        # The encoded reply to an accepted request; a CancelledError passes
        # through (it is no Exception), so a cancelled request gets none.
        handler = self._request_handlers.get(request.method)
        if handler is None:
            return self._failure(
                request.id,
                jsonrpc.METHOD_NOT_FOUND,
                f"method not found: {request.method}",
            )
        context = self._make_context(request.id, request.params)
        try:
            result = await self._invoke(handler, request.params, context)
        except errors.RequestError as error:
            return self._failure(request.id, error.code, error.message)
        except Exception:
            log.exception("request handler %r failed", request.method)
            return self._failure(
                request.id, jsonrpc.INTERNAL_ERROR, "internal error"
            )
        return self._encode(
            jsonrpc.Response(
                id=request.id, result=result, jsonrpc=jsonrpc.VERSION
            )
        )

    async def _notify(self, notification):
        handler = self._notification_handlers.get(notification.method)
        if handler is None:
            log.debug("no handler for notification %r", notification.method)
            return
        context = self._make_context(None, notification.params)
        try:
            await self._invoke(handler, notification.params, context)
        except Exception:
            log.exception("notification handler %r failed", notification.method)

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
            client_capabilities=self._client_capabilities,
            progress_token=token,
            outbox=self._outbox,
            encode=self._encode,
            request=self._request,
        )

    async def _write_outbox(self, transport, outbox):
        while True:
            message = await outbox.get()
            try:
                line = self._line_to_write(message)
                if line is not None:
                    await transport.write_line(line)
            finally:
                outbox.task_done()

    def _line_to_write(self, message):
        if not isinstance(message, Reply):
            return message
        if message.request_name not in self._owed:
            return None  # cancelled before it was written
        self._owed.discard(message.request_name)
        return message.line

    def _encode(self, envelope):
        return json.dumps(
            to_wire(envelope), ensure_ascii=False, separators=(",", ":")
        )

    def _failure(self, request_id, code, text):
        return self._encode(
            jsonrpc.ErrorResponse(
                error=jsonrpc.Error(code=code, message=text),
                jsonrpc=jsonrpc.VERSION,
                id=request_id,
            )
        )

    def _on_job_error(self, scheduler, context):
        log.error("job error: %r", context.get("exception"))
