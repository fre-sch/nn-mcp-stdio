# nn-mcp-stdio -- No nonsense stdio MCP server framework

Build a [Model Context Protocol](https://modelcontextprotocol.io) server
(protocol `2025-11-25`) by writing plain `async def` handlers and decorating
them. You annotate the arguments; the server derives the tool's `inputSchema`,
validates every call strictly, and hands your handler natural keyword
arguments. No pydantic, no code generation, no config.

## Goals

- A stdio MCP server, specification-conformant.
- **Strict validation.** Arguments are checked against the generated JSON Schema
  with `jsonschema` -- and never coerced. `{"n": "3"}` for an `int` is an error,
  not a silent `3`.
- Annotations and docstrings *are* the source of truth -- one obvious place for
  each fact, no duplication to keep in sync.

## Non-Goals

- HTTP, websocket (stdio only).
- Dependency injection.
- Storage layers.

## Install

```sh
pip install nn-mcp-stdio
```

Runtime dependencies: `nn-mcp-types` (MCP dataclasses + schema generation),
`jsonschema` (validation), and `aiojobs` (the handler scheduler).

## Implementation overview

The server is a small asyncio pipeline: a single **reader** parses each stdin
line and
dispatches by message shape; an `aiojobs` **scheduler** runs handlers
concurrently and in isolation; each handler enqueues its reply on an outbound
queue that a single **writer** drains to stdout. Logging goes to stderr, so it
never corrupts the protocol stream.

Adding a tool wires three pieces of `nn-mcp-types` together:

- your handler's **signature** -> a synthesised dataclass (parameters become
  fields, defaults and `Annotated` metadata carried through),
- that dataclass -> the tool's **`inputSchema`** (JSON Schema 2020-12),
- the handler's **docstring** -> the tool **description**, its **name** -> the
  tool name.

On `tools/call` the arguments dict is validated against that schema; on success
the values are reconstructed into the typed dataclass and the handler is called
with natural kwargs. `tools/list` is served from the registered tools, and
registering any tool advertises the `tools` capability at `initialize`.

## Adding tools

### The simplest tool

```python
import asyncio

from nn_mcp_stdio import Server

server = Server(name="calc", version="0.1.0")


@server.tool()
async def add(a: int, b: int) -> str:
    """Add two integers."""
    return str(a + b)


asyncio.run(server.run())
```

`add` is advertised with this `inputSchema` (arguments are closed by default --
see `strict_arguments`):

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "title": "add_arguments",
  "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
  "required": ["a", "b"],
  "additionalProperties": false
}
```

A call with `{"a": "2", "b": 3}` is rejected with `INVALID_PARAMS` (`"2"` is a
string) -- no silent coercion.

### Per-argument documentation and constraints

Descriptions and constraints are explicit, on the parameter, via
`SchemaAnnotation` -- never parsed out of the docstring:

```python
import typing

from nn_mcp_types.schema import SchemaAnnotation


@server.tool()
async def crop(
    url: typing.Annotated[str, SchemaAnnotation(description="Image URL")],
    width: typing.Annotated[
        int, SchemaAnnotation(description="Target width (px)", minimum=1)
    ] = 800,
) -> str:
    """Crop the image at `url` to `width` pixels."""
    ...
```

`width` has a default, so it is optional (absent from `required`) and its schema
carries `"minimum": 1` and `"default": 800`.

### A structured argument (nested dataclass)

A parameter may itself be a dataclass -- tool arguments are arbitrary JSON
objects, so nesting is natural. It renders as a `$ref`/`$defs`, and `from_wire`
reconstructs the instance before your handler runs:

```python
import dataclasses


@dataclasses.dataclass
class Point:
    x: float
    y: float


@server.tool()
async def distance(origin: Point, target: Point) -> str:
    """Euclidean distance between two points."""
    dx, dy = origin.x - target.x, origin.y - target.y
    return str((dx * dx + dy * dy) ** 0.5)
```

### Names, descriptions, and open arguments

`name` and `description` override the defaults; `strict_arguments=False` opts a
tool out of the closed-object default so unknown arguments are accepted:

```python
@server.tool(
    name="search_products",
    description="Search the catalog",
    strict_arguments=False,
)
async def _search(
    query: typing.Annotated[str, SchemaAnnotation(description="Free-text query")],
) -> str:
    ...
```

### Return types

The handler's return is mapped to a `CallToolResult`:

- `str` -> a single `TextContent`,
- a content block or a `list` of them (`nn_mcp_types.content`) -> passed
  through,
- a `CallToolResult` (`nn_mcp_types.tools`) -> used as-is, for full control,
- `None` -> empty content.

Returning anything else (a bare `int`, `dict`, or dataclass) raises `TypeError`
-- unless the tool opts into structured output (below).

```python
from nn_mcp_types import content, tools


@server.tool()
async def banner(text: str) -> content.TextContent:
    return content.TextContent(text=text.upper())


@server.tool()
async def report(kind: str) -> tools.CallToolResult:
    return tools.CallToolResult(
        content=[content.TextContent(text=f"report: {kind}")]
    )
```

### Structured output

Opt in with `structured_content=True`. The return (a dataclass or `dict`) becomes
the result's `structuredContent`, and a dataclass return annotation derives the
tool's `outputSchema`:

```python
import dataclasses


@dataclasses.dataclass
class Stats:
    count: int
    ok: bool


@server.tool(structured_content=True)
async def stats() -> Stats:
    """Report stats."""
    return Stats(count=3, ok=True)
```

`tools/call` then returns `{"content": [], "structuredContent": {"count": 3,
"ok": true}}`. The `content` array is left **empty** -- the spec's backward-compat
"serialize the JSON into a `TextContent` too" is only a SHOULD, and mirroring it
by default is wasteful for clients that read `structuredContent`. If you do need
the mirror (for an older client), return a `CallToolResult` yourself with both
`content` and `structured_content` set. A `-> dict` return is structured too, but
declares no `outputSchema`.

### Errors

- **Invalid arguments** (schema validation) and **unknown tool** are answered
  with JSON-RPC `INVALID_PARAMS` -- the call never reaches the handler.
- A **handler that raises** yields a `CallToolResult` with `is_error=True`
  carrying the exception message -- an in-band tool failure, not a protocol
  error.
- To answer with a **specific JSON-RPC error** instead, raise one from
  `nn_mcp_stdio.errors`:

```python
from nn_mcp_stdio import errors


@server.tool()
async def divide(a: int, b: int) -> str:
    if b == 0:
        raise errors.invalid_params("b must be non-zero")
    return str(a // b)
```

## Context (logging and progress)

A handler -- a tool, or a raw `@server.request`/`@server.notification` -- can
declare a parameter annotated `Context` (any name). The server injects the live
connection for that call; the parameter never appears in a tool's `inputSchema`.

```python
from nn_mcp_stdio import Context, Server

server = Server(name="jobs", version="0.1.0")


@server.tool()
async def crunch(rows: int, ctx: Context) -> str:
    """Crunch `rows` rows, reporting progress."""
    await ctx.info(f"starting {rows} rows", logger="crunch")
    for done in range(rows):
        await ctx.report_progress(done + 1, total=rows)
    return "done"
```

Through `Context` a handler talks back to the client mid-call:

- **Logging** -> `notifications/message`: `ctx.log(level, data, *, logger=None)`
  for the full RFC-5424 level set, with `ctx.debug`/`info`/`warning`/`error`
  shorthands. The server declares the `logging` capability.
- **Progress** -> `notifications/progress`:
  `ctx.report_progress(progress, total=None, message=None)`. It is a **no-op
  unless the request carried a `progressToken`** (in `params._meta`) -- the
  client opts into progress.
- **Metadata**: `ctx.request_id` (the in-flight request id) and `ctx.client`
  (the client's `Implementation`, captured at `initialize`).

Notifications are enqueued on the same single outbound stream as replies, so
they never interleave. All `Context` methods are `async`.

> Server-initiated *requests* (sampling, elicitation, roots) are a later phase;
> `Context` will grow `sample`/`elicit`/`list_roots` then.

## Resources

A resource is *identity*: a stable URI and a reader that returns its contents.
Unlike a tool it takes no arguments -- there is nothing to validate, and no
query. (Searching a collection is a *tool*, not a resource.) Register one with
`@server.resource(definition)`, where `definition` is a full
`nn_mcp_types.resources.Resource`:

```python
import json

from nn_mcp_stdio import Server
from nn_mcp_types import resources

server = Server(name="lib", version="0.1.0")


@server.resource(
    resources.Resource(
        uri="file:///config.json",
        name="config",
        title="The service configuration",
        mime_type="application/json",
    )
)
async def config() -> str:
    return json.dumps({"debug": True})
```

Unlike `@server.tool` -- whose handler's signature and docstring *are* the
definition -- a reader describes only its contents, not its identity or metadata.
So the decorator carries the whole `Resource` (there is nothing to infer from the
reader, and this is the only way to set `title`, `annotations`, `meta`, ...). The
`Resource` is listed by `resources/list` verbatim and read by `resources/read`
for its `uri`. The reader's return is mapped to a `ReadResourceResult`, filling
`uri`/`mimeType` from `definition`:

- `str` -> a single `TextResourceContents`,
- `bytes` -> a single `BlobResourceContents` (base64-encoded),
- a `TextResourceContents`/`BlobResourceContents` or a `list` of them
  (`nn_mcp_types.content`) -> used as the contents (full control -- e.g. a
  differing per-part `uri`),
- a `ReadResourceResult` (`nn_mcp_types.resources`) -> used as-is,
- anything else -> `TypeError`.

Reading an unregistered URI answers with JSON-RPC `-32002` ("Resource not
found") -- the reader is never reached. Registering any resource advertises the
`resources` capability at `initialize`. A reader may declare a `Context`
parameter, injected like any other handler's.

### Fixed resources (no reader)

When the contents are *fixed* -- a literal, or a file on disk -- there is no
logic to carry, so writing a reader per resource is just noise (and a decorator
in a loop closes over the loop variable, serving the wrong content). Register
them directly instead, passing the full `nn_mcp_types.resources.Resource` as the
definition (so you never re-spell its fields) plus the payload:

```python
import pathlib

from nn_mcp_types import resources
from nn_mcp_types.content import TextResourceContents

# A literal: `str` -> text, `bytes` -> base64 blob.
server.add_resource_from_literal(
    resources.Resource(
        uri="file:///config.json",
        name="config",
        title="The service configuration",
        mime_type="application/json",
    ),
    '{"debug": true}',
)

# A file, read lazily on every read. `describe_contents(path, data)` classifies
# the bytes -- returning `(mime_type, block_type)` -- so the framework need not
# guess from the (unreliable) file name.
server.add_resource_from_path(
    resources.Resource(uri="file:///README.md", name="readme"),
    pathlib.Path("README.md"),
    describe_contents=lambda path, data: ("text/markdown", TextResourceContents),
)
```

The `definition` is listed by `resources/list` verbatim (its `mime_type` is the
advertised *hint*), and read by `definition.uri`. For a file, `describe_contents`
runs lazily on each read -- so a content-sniffing classifier gets the actual
bytes, and an edited file is reflected on the next read -- and its
`(mime_type, block_type)` overrides `definition.mime_type` for the served
content. Without a classifier the file is served as a base64
`BlobResourceContents` typed by `definition.mime_type`; a file classified as
`TextResourceContents` is decoded as UTF-8. A non-`str`/`bytes` literal, or a
`block_type` that is neither contents class, raises `TypeError`.

> Static resources only for now. URI templates
> (`resources/templates/list`), `subscribe`/`unsubscribe`, and the
> `updated`/`list_changed` notifications are a later slice.

## Other handlers

Beyond tools, register any method or notification directly (a `Context`
parameter is injected here too):

```python
@server.request("ping")
async def ping(params):
    return {}


@server.notification("notifications/initialized")
async def initialized(params):
    ...
```

Handlers must be `async def`. `server.run()` reads until stdin EOF, then drains
in-flight handlers and their replies before returning.
