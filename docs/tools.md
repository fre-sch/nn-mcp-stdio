# Tools

A tool is an annotated `async def` you decorate with `@server.tool`. The
handler's signature and docstring *are* the definition: parameters become the
tool's `inputSchema`, the docstring becomes its description, the function name
becomes the tool name. On `tools/call` the arguments are validated against that
schema -- strictly, no coercion -- reconstructed into typed values, and passed
to the handler as natural keyword arguments.

## The simplest tool

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
see [Open arguments](#names-descriptions-and-open-arguments)):

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

Handlers must be `async def`; a non-coroutine raises `TypeError` at
registration. `@server.tool` works bare (`@server.tool`) or with options
(`@server.tool(name=..., strict_arguments=...)`).

## Per-argument documentation and constraints

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

## A structured argument (nested dataclass)

A parameter may itself be a dataclass -- tool arguments are arbitrary JSON
objects, so nesting is natural. It renders as a `$ref`/`$defs`, and the instance
is reconstructed before your handler runs:

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

## Names, descriptions, and open arguments

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

## Listing: pages of tools

`tools/list` returns tools in registration order, `page_size` at a time
(default 100). A page with more to come carries a `nextCursor`; the client sends
it back as `cursor` for the next page, and the last page has none. A server
with fewer tools than a page answers in one page, as if unpaginated.

```python
server = Server(name="demo", version="0.1.0", page_size=50)  # None: one page
```

The page size is the server's alone -- MCP gives the client no way to ask for
one. A malformed cursor is answered `INVALID_PARAMS`. The tool set is fixed at
start-up, so a client paging through it sees every tool exactly once.

## Return types

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

## Structured output

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

## Errors

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

A tool may also declare a `Context` parameter for logging and progress -- see
[Context](context.md).
