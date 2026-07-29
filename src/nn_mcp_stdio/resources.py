"""Turn an async reader function into a readable MCP resource.

A resource is a readable URI. A **direct** resource is a fixed URI (its
`definition` is a `Resource`); a **template** resource is a URI *shape* (its
`definition` is a `ResourceTemplate`) that reads any URI its RFC 6570 template
matches. `build_resource` / `build_template_resource` wrap an `async def` reader
with its wire definition. `Resource.read(uri, variables, context)` runs the
reader -- injecting the template `variables` the router extracted (empty for a
direct resource) plus an optional `Context` -- and maps its return to a
`ReadResourceResult`, filling `uri`/`mimeType` (from the concrete request URI and
the definition) so the reader repeats neither.
"""

import base64
import dataclasses
import inspect
import logging
import pathlib
import typing

from nn_mcp_types import resources as resource_types
from nn_mcp_types.content import BlobResourceContents, TextResourceContents

from nn_mcp_stdio.context import Context, context_parameter

log = logging.getLogger("nn_mcp_stdio")

# The concrete resource-contents classes a reader may return directly.
_CONTENTS = (TextResourceContents, BlobResourceContents)


@dataclasses.dataclass
class Resource:
    """A registered resource: its wire definition and its reader.

    `definition` is a `Resource` for a direct resource or a `ResourceTemplate`
    for a template resource; both carry the `mime_type` a wrapped return is typed
    with.
    """

    definition: resource_types.Resource | resource_types.ResourceTemplate
    reader: typing.Callable
    context_parameter: str | None = None

    async def read(
        self,
        uri: str,
        variables: dict[str, str] | None = None,
        context: Context | None = None,
    ) -> resource_types.ReadResourceResult:
        """Run the reader for `uri` and map its return to a `ReadResourceResult`.

        `variables` are the template variables the router extracted from `uri`
        (empty for a direct resource); they are injected as keyword arguments,
        mirroring tool-argument injection. A wrapped `str`/`bytes` return is
        typed by `uri` and the definition's MIME type. A reader failure
        propagates to the server, which answers with a JSON-RPC error -- a
        resource read has no in-band error result.
        """
        keyword_arguments = {}
        keyword_arguments.update(variables or {})
        if self.context_parameter is not None:
            keyword_arguments[self.context_parameter] = context
        result = await self.reader(**keyword_arguments)
        return self._as_read_result(uri, result)

    def _as_read_result(self, uri, result):
        if isinstance(result, resource_types.ReadResourceResult):
            return result  # full control
        if isinstance(result, _CONTENTS):
            return resource_types.ReadResourceResult(contents=[result])
        if isinstance(result, list) and all(
            isinstance(item, _CONTENTS) for item in result
        ):
            return resource_types.ReadResourceResult(contents=list(result))
        if isinstance(result, str):
            return resource_types.ReadResourceResult(
                contents=[self._text(uri, result)]
            )
        if isinstance(result, (bytes, bytearray)):
            return resource_types.ReadResourceResult(
                contents=[self._blob(uri, bytes(result))]
            )
        raise TypeError(
            f"unsupported resource return {type(result).__name__!r}: return a "
            "str, bytes, a resource-contents block (or list), or a "
            "ReadResourceResult"
        )

    def _text(self, uri, text):
        return TextResourceContents(
            uri=uri,
            text=text,
            mime_type=self.definition.mime_type,
        )

    def _blob(self, uri, data):
        return BlobResourceContents(
            uri=uri,
            blob=base64.b64encode(data).decode("ascii"),
            mime_type=self.definition.mime_type,
        )


def build_resource(
    reader: typing.Callable,
    definition: resource_types.Resource,
) -> Resource:
    """Build a `Resource` from an `async def` reader (see module docstring).

    `definition` is the full wire `Resource` -- the decorator carries the whole
    identity, since a reader describes only its contents, not its metadata.
    """
    if not inspect.iscoroutinefunction(reader):
        raise TypeError(
            f"resource reader {reader.__name__!r} must be `async def`"
        )
    return Resource(
        definition=definition,
        reader=reader,
        context_parameter=context_parameter(reader),
    )


def build_template_resource(
    reader: typing.Callable,
    definition: resource_types.ResourceTemplate,
) -> Resource:
    """Build a template `Resource` from an `async def` reader.

    `definition` is the full wire `ResourceTemplate`; its `uri_template` is the
    route the reader answers. The reader's parameters are the template's `{vars}`
    -- the router extracts them from the concrete URI and injects them by name --
    plus an optional `Context`. The return is mapped like any resource read.
    """
    if not inspect.iscoroutinefunction(reader):
        raise TypeError(
            f"resource template reader {reader.__name__!r} must be `async def`"
        )
    return Resource(
        definition=definition,
        reader=reader,
        context_parameter=context_parameter(reader),
    )


# A classifier for a file's bytes: given the path and the read data, name the
# MIME type and the contents block class (text or blob) the content maps to.
DescribeContents = typing.Callable[
    [pathlib.Path, bytes],
    tuple[str, type[TextResourceContents | BlobResourceContents]],
]


def build_literal_resource(
    definition: resource_types.Resource,
    content: str | bytes,
) -> Resource:
    """Build a static resource whose contents are a fixed literal.

    `definition` is the full wire `Resource` (listed verbatim). `content` is
    served on every read -- a `str` as `TextResourceContents`, `bytes` as a
    base64 `BlobResourceContents` -- with `uri`/`mimeType` from `definition`.
    """
    if not isinstance(content, (str, bytes, bytearray)):
        raise TypeError(
            "resource literal must be str or bytes, not "
            f"{type(content).__name__!r}"
        )

    async def reader():
        return content

    return Resource(definition=definition, reader=reader)


def build_path_resource(
    definition: resource_types.Resource,
    path: pathlib.Path,
    *,
    describe_contents: DescribeContents | None = None,
) -> Resource:
    """Build a static resource whose contents are read from a file.

    `definition` is the full wire `Resource` (listed verbatim). `path` is read
    lazily on every read. `describe_contents(path, data)` classifies the bytes --
    returning `(mime_type, block_class)` -- and so overrides `definition`'s MIME
    type for the served content; without it the file is served as a base64
    `BlobResourceContents` typed by `definition.mime_type`.
    """

    async def reader():
        data = path.read_bytes()
        if describe_contents is not None:
            mime_type, block_class = describe_contents(path, data)
        else:
            mime_type, block_class = definition.mime_type, BlobResourceContents
        return _content_block(definition.uri, data, mime_type, block_class)

    return Resource(definition=definition, reader=reader)


def _content_block(uri, data, mime_type, block_class):
    if block_class is TextResourceContents:
        return TextResourceContents(
            uri=uri, text=data.decode("utf-8"), mime_type=mime_type
        )
    if block_class is BlobResourceContents:
        return BlobResourceContents(
            uri=uri,
            blob=base64.b64encode(data).decode("ascii"),
            mime_type=mime_type,
        )
    raise TypeError(
        "describe_contents must return TextResourceContents or "
        f"BlobResourceContents, not "
        f"{getattr(block_class, '__name__', block_class)!r}"
    )
