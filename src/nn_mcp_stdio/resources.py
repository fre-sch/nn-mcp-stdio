"""Turn an async reader function into a readable MCP resource.

A resource is *identity*: a stable URI and a reader producing its contents.
Unlike a tool it takes no arguments -- there is nothing to validate on the way
in (searching a collection is a *tool*, not a resource). `build_resource` wraps
an `async def` reader
with the resource's wire definition; `Resource.read` runs the reader and maps its
return to a `ReadResourceResult`, filling `uri`/`mimeType` from the registration
so the reader repeats neither.
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
    """A registered resource: its wire definition and its reader."""

    definition: resource_types.Resource
    reader: typing.Callable
    context_parameter: str | None = None

    async def read(
        self, context: Context | None = None
    ) -> resource_types.ReadResourceResult:
        """Run the reader and map its return to a `ReadResourceResult`.

        A reader failure propagates to the server, which answers with a JSON-RPC
        error -- a resource read has no in-band error result.
        """
        keyword_arguments = {}
        if self.context_parameter is not None:
            keyword_arguments[self.context_parameter] = context
        result = await self.reader(**keyword_arguments)
        return self._as_read_result(result)

    def _as_read_result(self, result):
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
                contents=[self._text(result)]
            )
        if isinstance(result, (bytes, bytearray)):
            return resource_types.ReadResourceResult(
                contents=[self._blob(bytes(result))]
            )
        raise TypeError(
            f"unsupported resource return {type(result).__name__!r}: return a "
            "str, bytes, a resource-contents block (or list), or a "
            "ReadResourceResult"
        )

    def _text(self, text):
        return TextResourceContents(
            uri=self.definition.uri,
            text=text,
            mime_type=self.definition.mime_type,
        )

    def _blob(self, data):
        return BlobResourceContents(
            uri=self.definition.uri,
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
