# Resources

A resource is *identity*: a stable URI and contents to read. Unlike a tool it
takes no arguments -- there is nothing to validate, and no query. (Searching a
collection is a *tool*, not a resource.) Reading an unregistered URI answers
with JSON-RPC `-32002` ("Resource not found") -- the reader is never reached,
and registering any resource advertises the `resources` capability at
`initialize`.

Every registration carries a full `nn_mcp_types.resources.Resource` as its
*definition*. Unlike a tool -- whose handler's signature and docstring *are* the
definition -- a resource's contents describe only themselves, not the identity
or metadata. So the definition holds the whole identity (`uri`, `name`, `title`,
`mime_type`, `annotations`, `meta`, ...); it is listed by `resources/list`
verbatim and read by its `uri`.

There are three ways to register: a **dynamic reader** that computes contents on
each read, the **fixed** registrations for a literal or a file, and a **template**
-- a URI *shape* that reads any URI matching it.

`resources/read` is a router. It resolves the requested URI to the one provider
that claims it, most-specific-wins: an exact URI (any fixed or dynamic resource)
beats a template, and a more-specific template beats a less-specific one. So a
fixed `file:///project/last_build` shadows a `file:///project/{path}` template
for that one URI, while every other `file:///project/...` still routes to the
template.

## Dynamic resources (a reader)

`@server.resource(definition)` decorates an `async def` reader. The reader
returns the contents; `uri`/`mimeType` are filled from `definition` so the
reader repeats neither:

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

The reader's return is mapped to a `ReadResourceResult`:

- `str` -> a single `TextResourceContents`,
- `bytes` -> a single `BlobResourceContents` (base64-encoded),
- a `TextResourceContents`/`BlobResourceContents` or a `list` of them
  (`nn_mcp_types.content`) -> used as the contents (full control -- e.g. a
  differing per-part `uri`),
- a `ReadResourceResult` (`nn_mcp_types.resources`) -> used as-is,
- anything else -> `TypeError`.

Readers must be `async def`. A reader may declare a `Context` parameter, injected
like any other handler's -- see [Context](context.md). A reader failure
propagates to the server, which answers with a JSON-RPC error; a resource read
has no in-band error result (unlike a tool).

## Fixed resources (no reader)

When the contents are *fixed* -- a literal, or a file on disk -- there is no
logic to carry, so writing a reader per resource is just noise (and a decorator
in a loop closes over the loop variable, serving the wrong content). Register
them directly instead, passing the full `Resource` as the definition (so you
never re-spell its fields) plus the payload.

### A literal

`str` is served as text, `bytes` as a base64 blob:

```python
from nn_mcp_types import resources

server.add_resource_from_literal(
    resources.Resource(
        uri="file:///config.json",
        name="config",
        title="The service configuration",
        mime_type="application/json",
    ),
    '{"debug": true}',
)
```

### A file

The file is read lazily on every read -- so an edited file is reflected on the
next read. `describe_contents(path, data)` classifies the bytes -- returning
`(mime_type, block_type)` -- so the framework need not guess from the
(unreliable) file name:

```python
import pathlib

from nn_mcp_types import resources
from nn_mcp_types.content import TextResourceContents

server.add_resource_from_path(
    resources.Resource(uri="file:///README.md", name="readme"),
    pathlib.Path("README.md"),
    describe_contents=lambda path, data: ("text/markdown", TextResourceContents),
)
```

The classifier's `(mime_type, block_type)` overrides `definition.mime_type` for
the served content. A file classified as `TextResourceContents` is decoded as
UTF-8. Without a classifier the file is served as a base64 `BlobResourceContents`
typed by `definition.mime_type`. A non-`str`/`bytes` literal, or a `block_type`
that is neither contents class, raises `TypeError`.

## Resource templates (a parameterized reader)

A template describes resources whose URI space is too large or too dynamic to
enumerate -- a file tree, a record by id. `@server.resource_template(definition)`
decorates an `async def` reader against a `ResourceTemplate` whose `uri_template`
is an [RFC 6570](https://www.rfc-editor.org/rfc/rfc6570) URI template. The
template is advertised verbatim by `resources/templates/list`; the *client*
expands it to concrete URIs, and a `resources/read` of one routes back to the
reader with the template's variables extracted from the URI and injected by name
-- exactly as a tool's arguments are:

```python
from nn_mcp_stdio import Context, Server
from nn_mcp_types import resources

server = Server(name="lib", version="0.1.0")


@server.resource_template(
    resources.ResourceTemplate(
        uri_template="file:///project/{path}",
        name="project-file",
        mime_type="text/plain",
    )
)
async def project_file(path, ctx: Context) -> str:
    return read_project_file(path)  # `path` is the matched URI segment
```

The reader's parameters are the template's `{vars}`; it may also declare a
`Context`. Its return is mapped exactly as a dynamic reader's is, with the
concrete request URI (not the template) typing a wrapped `str`/`bytes`.

The supported template subset is RFC 6570 *simple variables*: `{var}` (one path
segment -- the value does not cross `/`), `{var*}` (a wildcard -- the value may
span segments), and a trailing `{?a,b}` query block. Registering a template with
any operator outside this subset is refused *at registration* -- a template the
router cannot reverse-match is never advertised.

## Not yet supported

`subscribe`/`unsubscribe`, the `updated`/`list_changed` notifications, and
pagination of `resources/list` / `resources/templates/list` are a later slice.
