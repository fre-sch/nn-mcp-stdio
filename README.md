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

Published to a private GitLab package registry, not to PyPI. The registry
serves its own packages and forwards every other name to PyPI, so it is the
only index to configure.

With uv, in the consuming project's `pyproject.toml`:

```toml
[[tool.uv.index]]
name = "nn-mcp"
url = "https://gitlab.com/api/v4/groups/141518299/-/packages/pypi/simple"
default = true
```

```sh
uv add nn-mcp-stdio
```

With pip:

```sh
pip install --index-url https://gitlab.com/api/v4/groups/141518299/-/packages/pypi/simple nn-mcp-stdio
```

The registry needs a token. A group deploy token scoped
`read_package_registry` is enough, in `~/.netrc`:

```
machine gitlab.com
login <deploy token username>
password <deploy token>
```

Runtime dependencies: `nn-mcp-types` (MCP dataclasses + schema
generation), `nn-rfc6570-router` (resource routing), `jsonschema`
(validation), and `aiojobs` (the handler scheduler).

## Implementation overview

The server is a small asyncio pipeline: a single **reader** parses each stdin
line and dispatches by message shape; an `aiojobs` **scheduler** runs handlers
concurrently and in isolation; each handler enqueues its reply on an outbound
queue that a single **writer** drains to stdout. Logging goes to stderr, so it
never corrupts the protocol stream.

A tool wires three pieces of `nn-mcp-types` together: your handler's
**signature** becomes a synthesised dataclass (parameters become fields,
defaults and `Annotated` metadata carried through); that dataclass becomes the
tool's **`inputSchema`** (JSON Schema 2020-12); the handler's **docstring**
becomes the tool description and its **name** the tool name. On `tools/call` the
arguments dict is validated against the schema, reconstructed into the typed
dataclass, and the handler is called with natural kwargs.

## Example

One server exposing a tool, two fixed resources (a literal and a file), a
dynamic resource read on demand, and a URI-template resource:

```python
import asyncio
import json
import pathlib
import typing

from nn_mcp_stdio import Context, Server
from nn_mcp_types import resources
from nn_mcp_types.content import TextResourceContents
from nn_mcp_types.schema import SchemaAnnotation

server = Server(name="demo", version="0.1.0")


@server.tool()
async def crop(
    url: typing.Annotated[str, SchemaAnnotation(description="Image URL")],
    width: typing.Annotated[
        int, SchemaAnnotation(description="Target width (px)", minimum=1)
    ] = 800,
    ctx: Context = None,
) -> str:
    """Crop the image at `url` to `width` pixels."""
    await ctx.info(f"cropping {url} to {width}px", logger="crop")
    return f"cropped {url} to {width}px"


# A literal resource: contents fixed in code, served on every read.
server.add_resource_from_literal(
    resources.Resource(
        uri="config://service",
        name="config",
        title="The service configuration",
        mime_type="application/json",
    ),
    json.dumps({"debug": True}),
)

# A file resource: read lazily on each read, so edits are reflected.
server.add_resource_from_path(
    resources.Resource(uri="file:///README.md", name="readme"),
    pathlib.Path("README.md"),
    describe_contents=lambda path, data: ("text/markdown", TextResourceContents),
)


# A dynamic resource: a reader computes the contents on each read.
@server.resource(
    resources.Resource(
        uri="clock://now",
        name="clock",
        mime_type="text/plain",
    )
)
async def clock() -> str:
    import datetime

    return datetime.datetime.now().isoformat()


# A resource template: a URI shape read on demand. The client expands the
# RFC 6570 template; a read routes back here with `{name}` extracted.
@server.resource_template(
    resources.ResourceTemplate(
        uri_template="greeting://{name}",
        name="greeting",
        mime_type="text/plain",
    )
)
async def greeting(name: str) -> str:
    return f"Hello, {name}!"


asyncio.run(server.run())
```

## Documentation

Guides to using each building block effectively:

- [Tools](docs/tools.md) -- annotated handlers, per-argument constraints,
  structured output, return types, and errors.
- [Resources](docs/resources.md) -- dynamic readers, the fixed literal/file
  registrations, and URI-template resources.
- [Context](docs/context.md) -- logging and progress back to the client
  mid-call, asking the user for input with `context.elicit`, and blocking work
  in a thread with `context.to_thread`.
- [Other handlers](docs/handlers.md) -- registering raw requests and
  notifications with `@server.request` / `@server.notification`.
