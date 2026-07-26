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
    uri: str,
    *,
    name: str | None = None,
    title: str | None = None,
    description: str | None = None,
    mime_type: str | None = None,
) -> Resource:
    """Build a `Resource` from an `async def` reader (see module docstring)."""
    if not inspect.iscoroutinefunction(reader):
        raise TypeError(
            f"resource reader {reader.__name__!r} must be `async def`"
        )
    definition = resource_types.Resource(
        uri=uri,
        name=name or reader.__name__,
        title=title,
        description=description or inspect.getdoc(reader),
        mime_type=mime_type,
    )
    return Resource(
        definition=definition,
        reader=reader,
        context_parameter=context_parameter(reader),
    )
