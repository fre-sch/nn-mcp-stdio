"""Turn an annotated handler function into an MCP tool.

A tool handler is a plain `async def` with annotated arguments. `build_tool`
synthesises a dataclass from its signature (fields = parameters, defaults and
`Annotated` metadata carried through) and generates the tool's `inputSchema`
from it. `Tool.call` validates an arguments dict against that schema -- strictly,
no coercion -- reconstructs the typed values, and invokes the handler with
natural keyword arguments. The handler's return is mapped to a `CallToolResult`.

See wiki discussion adding-tools and decision stdio-tool-definition-and-validation.
"""

import dataclasses
import inspect
import logging
import typing

from jsonschema.exceptions import best_match
from jsonschema.validators import Draft202012Validator

from nn_mcp_types import content
from nn_mcp_types import tools as tool_types
from nn_mcp_types.schema import SchemaAnnotation, get_schema
from nn_mcp_types.wire import from_wire, to_wire

from nn_mcp_stdio import errors
from nn_mcp_stdio.context import Context, context_parameter

log = logging.getLogger("nn_mcp_stdio")

# The concrete content-block classes a handler may return directly.
_CONTENT_BLOCKS = typing.get_args(content.ContentBlock)


@dataclasses.dataclass
class Tool:
    """A registered tool: its wire definition, its validator, and its handler."""

    definition: tool_types.Tool
    handler: typing.Callable
    arguments: type
    validator: Draft202012Validator
    context_parameter: str | None = None
    structured_content: bool = False

    async def call(
        self, arguments: dict | None, context: Context | None = None
    ) -> tool_types.CallToolResult:
        """Validate `arguments`, reconstruct them, and run the handler."""
        arguments = arguments or {}
        invalid = best_match(self.validator.iter_errors(arguments))
        if invalid is not None:
            raise errors.invalid_params(invalid.message)
        typed = from_wire(self.arguments, arguments)
        keyword_arguments = {
            field.name: getattr(typed, field.name)
            for field in dataclasses.fields(typed)
        }
        if self.context_parameter is not None:
            keyword_arguments[self.context_parameter] = context
        try:
            result = await self.handler(**keyword_arguments)
        except Exception as exception:
            log.exception("tool %r failed", self.definition.name)
            return _error_result(str(exception))
        return _as_call_result(result, self.structured_content)


def build_tool(
    handler: typing.Callable,
    *,
    name: str | None = None,
    description: str | None = None,
    strict_arguments: bool = True,
    structured_content: bool = False,
) -> Tool:
    """Build a `Tool` from an `async def` handler (see module docstring)."""
    if not inspect.iscoroutinefunction(handler):
        raise TypeError(
            f"tool handler {handler.__name__!r} must be `async def`"
        )
    context_name = context_parameter(handler)
    arguments = _synthesise_arguments(handler, strict_arguments, context_name)
    input_schema = get_schema(arguments)
    Draft202012Validator.check_schema(input_schema)
    definition = tool_types.Tool(
        name=name or handler.__name__,
        input_schema=input_schema,
        description=description or inspect.getdoc(handler),
        output_schema=_output_schema(handler) if structured_content else None,
    )
    return Tool(
        definition=definition,
        handler=handler,
        arguments=arguments,
        validator=Draft202012Validator(input_schema),
        context_parameter=context_name,
        structured_content=structured_content,
    )


def _output_schema(handler):
    # Derive an outputSchema from the return annotation when it is a dataclass;
    # a non-dataclass return (e.g. `dict`) is structured but self-describing.
    return_hint = _strip_annotated(
        typing.get_type_hints(handler, include_extras=True).get("return")
    )
    if dataclasses.is_dataclass(return_hint) and isinstance(return_hint, type):
        return get_schema(return_hint)
    return None


def _strip_annotated(hint):
    if typing.get_origin(hint) is typing.Annotated:
        return typing.get_args(hint)[0]
    return hint


def _synthesise_arguments(handler, strict_arguments, context_name):
    hints = typing.get_type_hints(handler, include_extras=True)
    fields = []
    for parameter in inspect.signature(handler).parameters.values():
        if parameter.name == context_name:
            continue  # injected by the server, not a wire argument
        annotation = hints.get(parameter.name, typing.Any)
        if parameter.default is inspect.Parameter.empty:
            fields.append((parameter.name, annotation))
        else:
            fields.append(
                (
                    parameter.name,
                    annotation,
                    dataclasses.field(default=parameter.default),
                )
            )
    namespace = {"__doc__": inspect.getdoc(handler)}
    if strict_arguments:
        namespace["SchemaConfig"] = _closed_object_config()
    return dataclasses.make_dataclass(
        f"{handler.__name__}_arguments", fields, namespace=namespace
    )


def _closed_object_config():
    # A class-level annotation dc_schema reads to close the arguments object
    # (reject unknown properties) -- see decision stdio-tool-definition-and-validation.
    return type(
        "SchemaConfig",
        (),
        {"annotation": SchemaAnnotation(additional_properties=False)},
    )


def _as_call_result(result, structured):
    if isinstance(result, tool_types.CallToolResult):
        return result  # full control (may carry both content and structured)
    if structured:
        return _structured_result(result)
    if result is None:
        return tool_types.CallToolResult(content=[])
    if isinstance(result, str):
        return tool_types.CallToolResult(
            content=[content.TextContent(text=result)]
        )
    if isinstance(result, _CONTENT_BLOCKS):
        return tool_types.CallToolResult(content=[result])
    if isinstance(result, list) and all(
        isinstance(item, _CONTENT_BLOCKS) for item in result
    ):
        return tool_types.CallToolResult(content=list(result))
    raise TypeError(
        f"unsupported tool return {type(result).__name__!r}: return a str, "
        "content block(s), or a CallToolResult (or set structured_content=True "
        "and return a dataclass/dict)"
    )


def _structured_result(result):
    # A structured tool returns an object -> structuredContent; content stays
    # empty (no wasteful backward-compat text mirror -- a handler wanting it
    # returns a CallToolResult with both fields).
    if result is None:
        return tool_types.CallToolResult(content=[])
    if isinstance(result, dict) or (
        dataclasses.is_dataclass(result) and not isinstance(result, type)
    ):
        return tool_types.CallToolResult(
            content=[], structured_content=to_wire(result)
        )
    raise TypeError(
        f"structured_content tool returned {type(result).__name__!r}: return a "
        "dataclass, a dict, or a CallToolResult"
    )


def _error_result(message):
    # A tool's own failure is a result with is_error, not a protocol error.
    return tool_types.CallToolResult(
        content=[content.TextContent(text=message)], is_error=True
    )
